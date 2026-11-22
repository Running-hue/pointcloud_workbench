"""Metric invariants, immutable exports, shared references and the live HTTP contract."""
from copy import deepcopy
import base64
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pointcloud_workbench.alignment import (fit_ground, body_frame, transform_points,
                                            alignment_status, VERSION, SIDE_FILES,
                                            enhanced_infrared)
from pointcloud_workbench.core import (np, Image, backproject, sha256, write_json,
                                      read_json, ply_bytes)
from pointcloud_workbench.tests.test_geometry import profile
from pointcloud_workbench.workspace import Workspace
from pointcloud_workbench.app import make_handler


class FrameMathTest(unittest.TestCase):
    def test_ir_enhancement_reveals_dim_texture_without_inventing_zero_signal(self):
        # A dim textured floor next to a much brighter animal.
        ir = np.tile(np.linspace(1, 16, 128).astype(np.uint16), (96, 1))
        ir[30:65, 40:90] = 8000
        ir[0, :10] = 0
        before = ir.copy()
        image = enhanced_infrared(ir)
        self.assertGreater(np.ptp(image[15, :, 0].astype(int)), 80)
        np.testing.assert_array_equal(image[0, :10], 0)
        np.testing.assert_array_equal(ir, before)

    def test_rotated_scene_preserves_scale_and_round_trips(self):
        # A 400 mm long, 200 mm wide shape above a known horizontal floor.
        body = np.array([[0,0,600], [400,0,650], [200,-100,620], [200,100,620], [0,0,0]], float)
        angle = .55
        rotation = np.array([[1,0,0],[0,np.cos(angle),-np.sin(angle)],[0,np.sin(angle),np.cos(angle)]])
        origin = np.array([120, -70, 1800])
        camera = body @ rotation.T + origin
        n = rotation[:, 2]
        frame = body_frame(-n, n @ origin, camera[0], camera[1])
        aligned = transform_points(camera, frame)
        np.testing.assert_allclose(aligned, body, atol=1e-10)
        m = np.asarray(frame['camera_to_body_4x4'])
        inv = np.asarray(frame['body_to_camera_4x4'])
        np.testing.assert_allclose(inv @ m, np.eye(4), atol=1e-10)
        self.assertAlmostEqual(np.linalg.det(m[:3,:3]), 1)
        self.assertAlmostEqual(np.linalg.norm(aligned[2]-aligned[3]), 200)
        self.assertAlmostEqual(frame['neck_tail_horizontal_span_mm'], 400)

    def test_ground_rejects_lines_small_support_and_nonplanar_regions(self):
        for points in [np.zeros((99,3)), np.column_stack([np.arange(200),np.zeros(200),np.zeros(200)]),
                       np.random.default_rng(5).uniform(0, 1000, (1000,3))]:
            with self.assertRaises(ValueError): fit_ground(points, 3)
        with self.assertRaises(ValueError): body_frame([0,0,1], 0, [0,0,0], [400,0,600])
        with self.assertRaises(ValueError): body_frame([0,0,1], 0, [0,0,600], [0,0,700])

    def test_ransac_recovers_a_noisy_floor_with_outliers(self):
        rng = np.random.default_rng(42)
        xy = rng.uniform(-500,500,(1800,2))
        p = np.column_stack([xy, 1400+.15*xy[:,0]-.2*xy[:,1]+rng.normal(0,.6,len(xy))])
        p = np.vstack([p,rng.uniform([-500,-500,300],[500,500,1300],(400,3))])
        n,d,inside,q=fit_ground(p,3)
        self.assertGreater(inside[:1800].mean(), .99)
        self.assertLess(q['inlier_rmse_mm'], 1)
        self.assertAlmostEqual(abs(n @ np.array([.15,-.2,-1])/np.linalg.norm([.15,-.2,-1])),1,places=5)
        self.assertLess(abs(n @ [0,0,1400]+d),1)


class AlignmentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.raw=self.root/'raw';self.results=self.root/'clouds'
        self.rel='camera/captures/pig_0001/capture'
        self.source=self.raw/self.rel;self.folder=self.results/self.rel
        self.source.mkdir(parents=True);self.folder.mkdir(parents=True)
        self.p=profile(100,80);self.p['intrinsic'].update(cx=50,cy=40)
        codes=np.full((80,100),2000,np.uint16);codes[25:56,25:76]=1200
        np.save(self.source/'depth_sensor_code_u16.npy',codes)
        np.save(self.source/'infrared.npy',np.arange(8000,dtype=np.uint16).reshape(80,100))
        record={'selected_capture':{'streams':{'depth':{'profile':self.p,'value_scale_mm_per_code':1}}}}
        write_json(self.source/'reconstruction.json',record)
        xyz,_,_=backproject(codes,1,self.p)
        mask=codes==1200;v,u=np.nonzero(mask);n=len(u)
        self.cloud={'xyz_mm':xyz[mask].astype(np.float32),'pixel_uv':np.column_stack([u,v]),
                    'depth_code':codes[mask],'infrared_raw':np.arange(n,dtype=float),
                    'infrared_valid':np.ones(n,bool),'rgb':np.full((n,3),120,np.uint8),
                    'rgb_valid':np.ones(n,bool),'rgb_uv':np.column_stack([u,v]),
                    'depth_discontinuity':np.zeros(n,bool)}
        np.savez_compressed(self.folder/'points.npz',**self.cloud)
        r={'coordinate_system':'native_depth_camera','unit':'mm','input_mode':'infrared',
           'pig_id':'0001','capture_id':'capture','reviewed':False,'segmentation_status':'draft',
           'source_dir':str(self.source),'segmentation_dir':str(self.root/'masks'/self.rel),
           'source_sha256':{name:sha256(self.source/name) for name in ['reconstruction.json','depth_sensor_code_u16.npy']},
           'stats':{'points':n,'valid_depth_ratio':1.,'rgb_valid_ratio':1.,
                    'xyz_min_mm':xyz[mask].min(axis=0).tolist(),'xyz_max_mm':xyz[mask].max(axis=0).tolist(),
                    'camera_axis_extent_mm':np.ptp(xyz[mask],axis=0).tolist()},'warnings':[],
           'camera':{'type':'itof','model':'Orbbec Femto Bolt'}}
        write_json(self.folder/'pointcloud.json',r)
        (self.folder/'cloud_ir_mm.ply').write_bytes(ply_bytes(self.cloud,self.cloud['rgb']))
        for name in ['mask_depth_check.png','rgb_projection_check.png']: Image.new('RGB',(100,80)).save(self.folder/name)
        (self.folder/'preview.html').write_text('<html>original</html>')
        self.ws=Workspace(self.root/'state',initial_results=self.results)
        self.base={'id':self.ws.state()['frames'][0]['id'],'dataset_id':self.ws.dataset_id}
        self.ctx=self.ws.alignment.context(self.base)
        self.payload={**self.base,'context_id':self.ctx['context_id'],
                      'ground_polygon':[[3,3],[96,3],[96,15],[3,15]],
                      'tail_uv':[30,40],'neck_uv':[70,40],'threshold_mm':3}

    def tearDown(self):
        if self.ws.worker:self.ws.worker.join(10)
        self.tmp.cleanup()

    def fit(self):return self.ws.alignment.fit(self.payload)

    def add_rgb(self):
        """A higher-resolution color sensor offset 30 mm from depth."""
        record = read_json(self.source/'reconstruction.json')
        streams = record['selected_capture']['streams']
        streams['depth']['identity'] = {'present':True, 'device_timestamp_us':'100000'}
        color_profile = deepcopy(self.p)
        for key in ('fx','fy','cx','cy','width','height'):
            color_profile['intrinsic'][key] *= 2
        streams['color'] = {'profile':color_profile,
                            'identity':{'present':True, 'device_timestamp_us':'100000'}}
        record['selected_capture']['timestamp_deltas'] = {'color_minus_depth_device_us':'0'}
        record['actual_profile_extrinsics'] = {'depth_to_color':{
            'rotation_row_major':np.eye(3).ravel().tolist(), 'translation_mm':[30,0,0]}}
        record['runtime'] = {'device_properties_at_session_start':{
            k:{'value':False} for k in ('depth_mirror','depth_flip','color_mirror','color_flip')}}
        write_json(self.source/'reconstruction.json', record)
        y,x = np.indices((160,200))
        Image.fromarray(np.stack([x,y,np.full_like(x,70)],axis=-1).astype(np.uint8)).save(self.source/'color.png')
        report = read_json(self.folder/'pointcloud.json')
        report['source_sha256'].update({name:sha256(self.source/name)
                                       for name in ('reconstruction.json','color.png')})
        write_json(self.folder/'pointcloud.json', report)
        return self.ws.alignment.context(self.base)

    @staticmethod
    def image(data_url):
        return np.array(Image.open(io.BytesIO(base64.b64decode(data_url.split(',',1)[1]))))

    def test_rgb_uses_calibrated_projection_and_keeps_the_native_annotation_grid(self):
        context = self.add_rgb()
        self.assertEqual(context['default_display_mode'],'rgb')
        self.assertTrue(context['rgb_display']['available'])
        # At native (50,40), Z=1200; color u=100+200*30/1200=105.
        # Resizing the RGB image would incorrectly yield red=100.
        rgb = self.image(context['rgb_image'])
        self.assertEqual(rgb.shape,(80,100,3))
        np.testing.assert_array_equal(rgb[40,50],[105,80,70])
        overlay = self.image(context['foreground_overlay'])
        self.assertEqual(overlay.shape,(80,100,4))
        self.assertEqual(overlay[5,5,3],0)
        self.assertGreater(overlay[40,50,3],0)
        self.payload['context_id'] = context['context_id']
        ir = self.ws.alignment.fit({**self.payload,'display_mode':'ir','display_brightness':1})
        rgb_fit = self.ws.alignment.fit({**self.payload,'display_mode':'rgb','display_brightness':2})
        self.assertEqual(ir['record']['frame'],rgb_fit['record']['frame'])
        self.assertEqual(ir['record']['reference_id'],rgb_fit['record']['reference_id'])
        self.assertEqual(rgb_fit['record']['check_image_brightness'],2)
        self.assertNotEqual(ir['check_image'],rgb_fit['check_image'])

    def test_missing_rgb_falls_back_with_an_explanation(self):
        self.assertFalse(self.ctx['rgb_display']['available'])
        self.assertIn('缺少',self.ctx['rgb_display']['reason'])
        self.assertEqual(self.ctx['default_display_mode'],'ir_enhanced')
        self.assertIsNone(self.ctx['rgb_image'])
        self.assertTrue(self.fit()['candidate_id'])

    def test_unverified_rgb_does_not_disable_depth_alignment(self):
        self.add_rgb()
        original = read_json(self.source/'reconstruction.json')
        for kind in ('unsynced','mirrored','unknown_mirror','wrong_size','changed_rgb'):
            with self.subTest(kind=kind):
                record = deepcopy(original)
                Image.new('RGB',(200,160),(30,40,50)).save(self.source/'color.png')
                if kind=='unsynced':
                    record['selected_capture']['streams']['color']['identity']['device_timestamp_us']='130000'
                    record['selected_capture']['timestamp_deltas']['color_minus_depth_device_us']='30000'
                elif kind=='mirrored':
                    record['runtime']['device_properties_at_session_start']['color_mirror']['value']=True
                elif kind=='unknown_mirror':
                    del record['runtime']['device_properties_at_session_start']['color_mirror']
                elif kind=='wrong_size':
                    Image.new('RGB',(100,80)).save(self.source/'color.png')
                write_json(self.source/'reconstruction.json',record)
                report=read_json(self.folder/'pointcloud.json')
                report['source_sha256'].update({name:sha256(self.source/name)
                                               for name in ('reconstruction.json','color.png')})
                write_json(self.folder/'pointcloud.json',report)
                if kind=='changed_rgb':
                    Image.new('RGB',(200,160),(90,60,20)).save(self.source/'color.png')
                context=self.ws.alignment.context(self.base)
                self.assertFalse(context['rgb_display']['available'])
                self.assertTrue(context['rgb_display']['reason'])
                fitted=self.ws.alignment.fit({**self.payload,'context_id':context['context_id']})
                self.assertTrue(fitted['candidate_id'])

    def test_changing_an_rgb_annotation_source_invalidates_the_context(self):
        context=self.add_rgb()
        Image.new('RGB',(200,160),(1,2,3)).save(self.source/'color.png')
        with self.assertRaisesRegex(ValueError,'原始数据已变化'):
            self.ws.alignment.fit({**self.payload,'context_id':context['context_id']})

    def test_invalid_display_controls_are_rejected(self):
        for extra in ({'display_mode':'rgb'},{'display_brightness':float('nan')},
                      {'display_brightness':10}):
            with self.assertRaises(ValueError):self.ws.alignment.fit({**self.payload,**extra})

    def save(self, name='aligned'):
        fitted=self.fit()
        request={**self.payload,'candidate_id':fitted['candidate_id'],'parent':str(self.root),
                 'name':name,'floor_confirmed':True,'anchors_confirmed':True}
        return self.ws.alignment.save(request),request

    def test_export_reopens_both_frames_and_preserves_all_input_bytes(self):
        before={str(p):sha256(p) for root in [self.raw,self.results] for p in root.rglob('*') if p.is_file()}
        saved,_=self.save();dest=Path(saved['output'])/self.rel
        self.assertTrue(alignment_status(dest))
        record=read_json(dest/'alignment.json')
        self.assertTrue(record['confirmed']);self.assertFalse(record['segmentation_reviewed'])
        self.assertEqual(record['pig_id'],'0001')
        with np.load(dest/'body_points.npz') as body:
            self.assertAlmostEqual(np.ptp(body['xyz_mm'][:,0]),600)
            self.assertAlmostEqual(np.ptp(body['xyz_mm'][:,1]),360)
            np.testing.assert_allclose(body['xyz_mm'][:,2],800,atol=1e-3)
            np.testing.assert_array_equal(body['pixel_uv'],self.cloud['pixel_uv'])
            np.testing.assert_array_equal(body['source_point_index'],np.arange(len(body['xyz_mm'])))
        self.assertEqual(before,{str(p):sha256(p) for root in [self.raw,self.results] for p in root.rglob('*') if p.is_file()})
        state=self.ws.open_results(saved['output'])
        self.assertTrue(state['frames'][0]['has_alignment'])
        self.assertTrue(state['alignment_source'])
        frame_id=state['frames'][0]['id']
        self.assertEqual(self.ws.media(frame_id,'body_points.npz'),dest/'body_points.npz')
        self.ws.start({'dataset_id':state['dataset_id'],'ids':[frame_id],'parent':str(self.root),'name':'copied'},'export')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        for name in SIDE_FILES:self.assertEqual(sha256(dest/name),sha256(self.root/'copied'/self.rel/name))

    def test_same_capture_rgb_and_ir_reuse_exactly_the_same_transform(self):
        first,_=self.save()
        other=self.root/'rgb';shutil.copytree(self.results,other)
        folder=other/self.rel;r=read_json(folder/'pointcloud.json');r['input_mode']='rgb'
        r['camera']={'type':'stereo','model':'Orbbec Gemini 336L'}
        write_json(folder/'pointcloud.json',r)
        (folder/'cloud_ir_mm.ply').rename(folder/'cloud_rgb_mm.ply')
        state=self.ws.open_results(str(other))
        base={'id':state['frames'][0]['id'],'dataset_id':state['dataset_id']}
        context=self.ws.alignment.context(base)
        self.assertEqual(context['reference']['reference_id'],first['reference_id'])
        fitted=self.ws.alignment.fit({**base,'context_id':context['context_id'],'reuse_reference':True})
        self.assertTrue(fitted['record']['reused_reference'])
        self.assertEqual(fitted['record']['frame'],read_json(Path(first['output'])/self.rel/'alignment.json')['frame'])
        self.assertEqual(fitted['record']['input_mode'],'rgb')

    def test_inline_save_keeps_batch_restores_associations_and_exports_aligned_clouds(self):
        other_rel='camera/captures/pig_0002/capture'
        other=self.results/other_rel
        shutil.copytree(self.folder,other)
        state=self.ws.open_results(str(self.results))
        self.base={**self.base,'dataset_id':state['dataset_id']}
        self.ctx=self.ws.alignment.context(self.base)
        self.payload.update(self.base,context_id=self.ctx['context_id'])
        before={str(p):sha256(p) for p in self.results.rglob('*') if p.is_file()}
        fitted=self.fit()
        saved=self.ws.alignment.save({**self.payload,'candidate_id':fitted['candidate_id'],
            'parent':str(self.root),'name':'inline','floor_confirmed':True,'anchors_confirmed':True,
            'attach_to_workspace':True})
        state=self.ws.state()
        self.assertTrue(saved['attached'])
        self.assertEqual(state['dataset_id'],self.base['dataset_id'])
        self.assertEqual(state['results'],str(self.results.resolve()))
        self.assertEqual(len(state['frames']),2)
        self.assertEqual(sum(f['has_alignment'] for f in state['frames']),1)
        self.assertEqual(self.ws.media(self.base['id'],'body_points.npz'),self.root.resolve()/'inline'/self.rel/'body_points.npz')
        restored=Workspace(self.root/'state',initial_results=self.results)
        self.assertEqual(len(restored.state()['frames']),2)
        self.assertTrue(restored.row(self.base['id'])['has_alignment'])
        self.assertTrue(restored.row(self.base['id'])['alignment_revision'])
        restored.start({'dataset_id':restored.dataset_id,'ids':[f['id'] for f in restored.state()['frames']],
                        'parent':str(self.root),'name':'combined'},'export')
        restored.worker.join(10)
        self.assertEqual(restored.job['status'],'completed',restored.job)
        self.assertTrue(alignment_status(self.root/'combined'/self.rel))
        self.assertTrue((self.root/'combined'/other_rel/'points.npz').is_file())
        self.assertEqual(before,{str(p):sha256(p) for p in self.results.rglob('*') if p.is_file()})

    def test_missing_or_changed_inline_alignment_falls_back_without_accepting_stale_geometry(self):
        fitted=self.fit()
        saved=self.ws.alignment.save({**self.payload,'candidate_id':fitted['candidate_id'],
            'parent':str(self.root),'name':'inline','floor_confirmed':True,'anchors_confirmed':True,
            'attach_to_workspace':True})
        dest=Path(saved['output'])/self.rel
        body_bytes=(dest/'body_points.npz').read_bytes()
        (dest/'body_points.npz').unlink()
        state=self.ws.open_results(str(self.results))
        self.assertFalse(state['frames'][0]['has_alignment'])
        self.assertIn('已退回相机点云',state['warning'])
        self.assertEqual(self.ws.media(self.base['id'],'points.npz'),self.folder.resolve()/'points.npz')
        (dest/'body_points.npz').write_bytes(body_bytes)
        with (self.folder/'points.npz').open('ab') as f:
            f.write(b'changed original cloud')
        state=self.ws.open_results(str(self.results))
        self.assertFalse(state['frames'][0]['has_alignment'])
        self.assertIn('已退回相机点云',state['warning'])

    def test_reference_can_be_recovered_from_portable_aligned_export(self):
        saved,_=self.save();self.ws.open_results(saved['output'])
        shutil.rmtree(self.root/'state/alignment_references')
        base={'id':self.ws.state()['frames'][0]['id'],'dataset_id':self.ws.dataset_id}
        context=self.ws.alignment.context(base)
        self.assertEqual(context['reference']['reference_id'],saved['reference_id'])

    def test_export_upgrades_legacy_body_preview_without_modifying_source_geometry(self):
        saved,_=self.save()
        folder=Path(saved['output'])/self.rel
        (folder/'body_preview.html').write_text('<html>legacy body preview</html>')
        before={name:sha256(folder/name) for name in SIDE_FILES}
        state=self.ws.open_results(saved['output'])
        self.ws.start({'dataset_id':state['dataset_id'],'ids':[state['frames'][0]['id']],
            'parent':str(self.root),'name':'updated-body-preview'},'export')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        dest=self.root/'updated-body-preview'/self.rel
        self.assertIn('id="toggleInfo"',(dest/'body_preview.html').read_text())
        self.assertEqual(before,{name:sha256(folder/name) for name in SIDE_FILES})
        for name in SIDE_FILES-{'body_preview.html'}:
            self.assertEqual(sha256(dest/name),before[name])

    def test_aligned_export_requires_quality_selection_and_keeps_portable_review(self):
        saved,_=self.save()
        self.ws.open_results(saved['output'])
        frame=self.ws.state()['frames'][0]
        payload={'dataset_id':self.ws.dataset_id,'ids':[frame['id']],'parent':str(self.root),
                 'name':'aligned-only','aligned_only':True}
        with self.assertRaisesRegex(ValueError,'默认只导出'):
            self.ws.start(payload,'export')
        self.ws.start({**payload,'name':'pending-explicit','include_pending':True},'export')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        self.ws.review_cloud({'dataset_id':self.ws.dataset_id,'id':frame['id'],'decision':'keep'})
        self.ws.start(payload,'export');self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        output=self.root/'aligned-only'
        manifest=read_json(output/'pointcloud_dataset.json')
        self.assertTrue(manifest['aligned_only'])
        self.assertEqual(manifest['frames'][0]['cloud_decision'],'keep')
        self.assertIn('body_preview.html',(output/'index.html').read_text())
        reopened=Workspace(self.root/'portable-state',initial_results=output)
        self.assertTrue(reopened.state()['frames'][0]['has_alignment'])
        self.assertEqual(reopened.state()['frames'][0]['cloud_decision'],'keep')
        self.ws.review_cloud({'dataset_id':self.ws.dataset_id,'id':frame['id'],'decision':'reject'})
        with self.assertRaisesRegex(ValueError,'淘汰'):
            self.ws.start({**payload,'name':'blocked'},'export')

    def test_confirm_is_durable_without_a_destination_and_batch_export_keeps_all_screening_records(self):
        for pig in ('0002','0003'):
            shutil.copytree(self.folder, self.results/self.rel.replace('0001',pig))
        state=self.ws.open_results(str(self.results))
        self.base['dataset_id']=state['dataset_id']
        self.ctx=self.ws.alignment.context(self.base)
        self.payload.update(self.base,context_id=self.ctx['context_id'])
        before={str(p):sha256(p) for root in (self.raw,self.results) for p in root.rglob('*') if p.is_file()}
        export_parent=self.ws.state()['export_parent']
        self.ws.review_cloud({**self.base,'decision':'reject'})
        fitted=self.fit()
        confirmed=self.ws.alignment.confirm({**self.payload,'candidate_id':fitted['candidate_id'],
            'alignment_confirmed':True})
        self.assertTrue(confirmed['cached'])
        self.assertEqual(confirmed['cloud_decision'],'keep')
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'],'keep')
        portable=read_json(Path(confirmed['output'])/self.rel/'cloud_review.json')
        self.assertEqual(portable['decision'],'keep')
        self.assertEqual(portable['points_sha256'],sha256(self.folder/'points.npz'))
        self.assertTrue(Path(confirmed['output']).is_relative_to(self.ws.state_dir/'alignment_cache'))
        self.assertEqual(self.ws.state()['export_parent'],export_parent)
        self.assertEqual(self.ws.state()['results'],str(self.results.resolve()))
        restored=Workspace(self.ws.state_dir,initial_results=self.results)
        rows=restored.state()['frames']
        self.assertTrue(rows[0]['alignment_cached'])
        self.assertEqual(rows[0]['cloud_decision'],'keep')
        ctx=restored.alignment.context({'dataset_id':restored.dataset_id,'id':rows[0]['id']})
        self.assertTrue(ctx['saved_alignment']['confirmed'])
        for row,decision in zip(rows,('keep','reject','pending')):
            restored.review_cloud({'dataset_id':restored.dataset_id,'id':row['id'],'decision':decision})
        restored.start({'dataset_id':restored.dataset_id,'ids':[rows[0]['id']],
            'scope_ids':[r['id'] for r in rows],'parent':str(self.root),'name':'one-batch','aligned_only':True},'export')
        restored.worker.join(10)
        self.assertEqual(restored.job['status'],'completed',restored.job)
        output=self.root/'one-batch'
        self.assertFalse(restored.state()['frames'][0]['alignment_cached'])
        self.assertEqual(restored.media(rows[0]['id'],'body_points.npz'),output.resolve()/self.rel/'body_points.npz')
        records=read_json(output/'cloud_screening.json')['frames']
        self.assertEqual([r['decision'] for r in records],['keep','reject','pending'])
        self.assertEqual([r['exported'] for r in records],[True,False,False])
        self.assertTrue((output/'cloud_screening.csv').is_file())
        self.assertTrue(alignment_status(output/self.rel))
        self.assertEqual(before,{str(p):sha256(p) for root in (self.raw,self.results) for p in root.rglob('*') if p.is_file()})

    def test_confirmation_requires_review_and_never_claims_success_when_persistence_fails(self):
        from unittest.mock import patch
        fitted=self.fit()
        payload={**self.payload,'candidate_id':fitted['candidate_id']}
        with self.assertRaisesRegex(ValueError,'确认对齐并保留'):
            self.ws.alignment.confirm(payload)
        self.ws.review_cloud({**self.base,'decision':'reject'})
        original_folder=self.ws.results[self.rel]['folder']
        original_settings=(self.ws.state_dir/'settings.json').read_bytes()
        with patch.object(self.ws,'remember',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError,'持久|保存'):
                self.ws.alignment.confirm({**payload,'alignment_confirmed':True})
        self.assertEqual(self.ws.results[self.rel]['folder'],original_folder)
        self.assertFalse(self.ws.state()['frames'][0]['has_alignment'])
        self.assertEqual(self.ws.row(self.base['id'])['cloud_decision'],'reject')
        self.assertEqual((self.ws.state_dir/'settings.json').read_bytes(),original_settings)
        self.assertFalse(list((self.ws.state_dir/'alignment_references').glob('*.json')))
        self.assertEqual(list((self.ws.state_dir/'alignment_cache').iterdir()),[])
        restored=Workspace(self.ws.state_dir,initial_results=self.results)
        self.assertEqual(restored.state()['frames'][0]['cloud_decision'],'reject')

    def test_failed_reconfirmation_preserves_previous_alignment_and_quality(self):
        from unittest.mock import patch
        fitted=self.fit()
        first=self.ws.alignment.confirm({**self.payload,'candidate_id':fitted['candidate_id'],
            'alignment_confirmed':True})
        self.ws.review_cloud({**self.base,'decision':'pending'})
        self.ctx=self.ws.alignment.context(self.base)
        self.payload['context_id']=self.ctx['context_id']
        fitted=self.fit()
        with patch.object(self.ws,'remember',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError,'持久|保存'):
                self.ws.alignment.confirm({**self.payload,'candidate_id':fitted['candidate_id'],
                    'alignment_confirmed':True})
        restored=Workspace(self.ws.state_dir,initial_results=self.results)
        row=restored.state()['frames'][0]
        self.assertTrue(row['has_alignment'])
        self.assertEqual(row['cloud_decision'],'pending')
        self.assertEqual(restored.results[self.rel]['folder'],Path(first['output'])/self.rel)
        self.assertEqual(len(list((self.ws.state_dir/'alignment_cache').iterdir())),1)

    def test_grouped_alignment_export_reopens_original_ids_statuses_and_cached_associations(self):
        from urllib.parse import unquote
        for pig in ('0002','0003','0004'):
            rel=self.rel.replace('0001',pig)
            shutil.copytree(self.source,self.raw/rel)
            shutil.copytree(self.folder,self.results/rel)
            report=read_json(self.results/rel/'pointcloud.json')
            report.update(pig_id=pig,source_dir=str(self.raw/rel))
            write_json(self.results/rel/'pointcloud.json',report)
        state=self.ws.open_results(str(self.results));rows=state['frames']
        original={str(p):sha256(p) for root in (self.raw,self.results) for p in root.rglob('*') if p.is_file()}
        for row,decision in zip(rows,('keep','pending','reject','reject')):
            base={'dataset_id':self.ws.dataset_id,'id':row['id']}
            if row['pig_id']!='0004':
                context=self.ws.alignment.context(base)
                request={**self.payload,**base,'context_id':context['context_id']}
                fitted=self.ws.alignment.fit(request)
                self.ws.alignment.confirm({**request,'candidate_id':fitted['candidate_id'],'alignment_confirmed':True})
            self.ws.review_cloud({**base,'decision':decision})
        self.ws.start({'dataset_id':self.ws.dataset_id,'ids':[r['id'] for r in rows[:3]],
            'scope_ids':[r['id'] for r in rows],'parent':str(self.root),'name':'grouped',
            'aligned_only':True,'decisions':['keep','pending','reject'],'group_by_decision':True},'export')
        self.ws.worker.join(10)
        self.assertEqual(self.ws.job['status'],'completed',self.ws.job)
        output=self.root/'grouped';manifest=read_json(output/'pointcloud_dataset.json')
        self.assertTrue(manifest['group_by_decision'])
        by_rel={r['relative_dir']:r for r in manifest['frames']}
        for row,label,decision in zip(rows,('保留','待定','淘汰'),('keep','pending','reject')):
            rel=row['relative_dir'];storage=f'{label}/{rel}';dest=output/storage
            self.assertEqual(by_rel[rel]['storage_relative_dir'],storage)
            self.assertEqual(by_rel[rel]['cloud_decision'],decision)
            self.assertEqual(read_json(dest/'cloud_review.json')['relative_dir'],rel)
            self.assertTrue(alignment_status(dest))
            self.assertEqual(sha256(dest/'points.npz'),sha256(self.results/rel/'points.npz'))
            self.assertIn(storage,unquote((output/'index.html').read_text()))
        screening=read_json(output/'cloud_screening.json')['frames']
        self.assertEqual([r['exported'] for r in screening],[True,True,True,False])
        self.assertEqual(screening[-1]['reason'],'尚未对齐')
        portable=Workspace(self.root/'portable-grouped',initial_results=output)
        self.assertEqual([r['relative_dir'] for r in portable.state()['frames']], [r['relative_dir'] for r in rows[:3]])
        self.assertEqual([r['cloud_decision'] for r in portable.state()['frames']],['keep','pending','reject'])
        subgroup=Workspace(self.root/'portable-reject',initial_results=output/'淘汰')
        self.assertEqual(subgroup.state()['frames'][0]['relative_dir'],rows[2]['relative_dir'])
        self.assertEqual(subgroup.state()['frames'][0]['cloud_decision'],'reject')
        restored=Workspace(self.ws.state_dir,initial_results=self.results)
        for row,label in zip(rows,('保留','待定','淘汰')):
            current=restored.row(row['id'])
            self.assertTrue(current['has_alignment'])
            self.assertFalse(current['alignment_cached'])
            self.assertEqual(restored.media(row['id'],'body_points.npz'),output.resolve()/label/row['relative_dir']/'body_points.npz')
            context=portable.alignment.context({'dataset_id':portable.dataset_id,'id':row['id']})
            self.assertTrue(context['saved_alignment']['confirmed'])
        portable.start({'dataset_id':portable.dataset_id,'ids':[r['id'] for r in portable.state()['frames']],
            'parent':str(self.root),'name':'regrouped','decisions':['keep','pending','reject'],
            'aligned_only':True,'group_by_decision':True},'export')
        portable.worker.join(10)
        self.assertEqual(portable.job['status'],'completed',portable.job)
        self.assertTrue((self.root/'regrouped/淘汰'/rows[2]['relative_dir']/'cloud_body_mm.ply').is_file())
        self.assertEqual(original,{str(p):sha256(p) for root in (self.raw,self.results) for p in root.rglob('*') if p.is_file()})

    def test_review_required_stale_results_and_protected_directories(self):
        fitted=self.fit()
        p={**self.payload,'candidate_id':fitted['candidate_id'],'parent':str(self.root),'name':'unreviewed'}
        with self.assertRaisesRegex(ValueError,'勾选'):self.ws.alignment.save(p)
        p.update(floor_confirmed=True,anchors_confirmed=True,parent=str(self.raw))
        with self.assertRaisesRegex(ValueError,'独立'):self.ws.alignment.save(p)
        self.assertFalse((self.raw/'unreviewed').exists())
        self.ws.alignment.fit(self.payload)
        with self.assertRaisesRegex(ValueError,'已改变'):self.ws.alignment.save({**p,'parent':str(self.root)})
        with self.assertRaisesRegex(ValueError,'目录已切换'):self.ws.alignment.fit({**self.payload,'dataset_id':'old'})
        self.source.joinpath('depth_sensor_code_u16.npy').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'原始数据已变化'):self.ws.alignment.fit(self.payload)

    def test_missing_depth_background_endpoints_and_wrong_raw_root_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'可见范围|相距'):self.ws.alignment.fit({**self.payload,'tail_uv':[4,4]})
        with self.assertRaisesRegex(ValueError,'匹配'):self.ws.alignment.context({**self.base,'source_root':str(self.root/'absent')})
        with self.assertRaises(ValueError):self.ws.alignment.fit({**self.payload,'ground_polygon':[[0,0]]})
        with self.assertRaises(ValueError):self.ws.alignment.fit({**self.payload,'threshold_mm':float('nan')})
        ctx=self.ws.alignment.contexts[self.ctx['context_id']]
        ctx['valid'][40,30]=False
        with self.assertRaisesRegex(ValueError,'没有有效深度'):self.fit()

    def test_mutated_cloud_invalidates_the_alignment_context(self):
        with open(self.folder/'points.npz','ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(ValueError,'点云已变化'):self.fit()

    def test_relocated_raw_inputs_work_without_the_old_segmentation_directory(self):
        report=read_json(self.folder/'pointcloud.json')
        report['source_dir']=str(self.root/'old-disk'/self.rel)
        write_json(self.folder/'pointcloud.json',report)
        self.ws.settings['source']=str(self.raw)
        context=self.ws.alignment.context(self.base)
        self.assertEqual(Path(context['source_dir']),self.source.resolve())
        self.assertEqual(self.ws.state()['alignment_source'],str(self.raw))

    def test_http_contract_preview_executes_and_saved_body_links_resolve(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.ws,'token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def post(path,data,token='token'):
            req=Request(url+'/api/'+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-App-Token':token})
            with urlopen(req) as response:return json.load(response)
        try:
            page=urlopen(url+'/alignment').read().decode();self.assertIn('/alignment.js',page)
            self.assertNotIn('__TOKEN__',page)
            with self.assertRaises(HTTPError):post('alignment/fit',self.payload,'wrong')
            context=post('alignment/context',self.base)
            fitted=post('alignment/fit',{**self.payload,'context_id':context['context_id']})
            preview=urlopen(url+'/api/alignment/preview?'+urlencode({**self.base,'context_id':context['context_id'],'candidate_id':fitted['candidate_id']})).read().decode()
            result=subprocess.run(['node',str(Path(__file__).with_name('body_view_runtime.cjs'))],input=preview,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            saved,_=self.save();state=self.ws.open_results(saved['output'])
            body_page=Path(saved['output'])/self.rel/'body_preview.html'
            body_page.write_text('<html>legacy body preview</html>')
            before=sha256(body_page)
            page=urlopen(url+'/api/media?'+urlencode({'dataset_id':state['dataset_id'],'id':state['frames'][0]['id'],'file':'body_preview.html'})).read().decode()
            self.assertIn('file=body_points.npz',page)
            self.assertIn('file=cloud_body_mm.ply',page)
            self.assertIn('id="toggleInfo"',page)
            self.assertNotIn('legacy body preview',page)
            self.assertEqual(sha256(body_page),before)
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
