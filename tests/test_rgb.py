from pathlib import Path
import shutil
import tempfile
import unittest

from pointcloud_workbench.core import (np, Image, read_json, write_json, sha256, reconstruct,
                                       export_frame, display_ir)
from pointcloud_workbench.viewer import write_viewer
from pointcloud_workbench.workspace import Workspace
from segmentation_workbench.geometry import prepare_frame
from segmentation_workbench.tests.geometry_fixture import calibration, profile


class RGBCloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source, self.seg = self.root/'raw', self.root/'masks'
        self.source.mkdir(); self.seg.mkdir()
        color, aligned = profile(9,7,fx=100,cx=4), profile(7,5,fx=100,cx=3)
        color['intrinsic'].update(fy=100,cy=3)
        aligned['intrinsic'].update(fy=100,cy=2)
        self.record = calibration(self.source, source=color, target=aligned, ir_size=(3,2), scale=.25)
        self.record['selected_capture']['timestamp_deltas']={'color_minus_depth_device_us':'0'}
        self.record['runtime']={'device_properties_at_session_start':{
            name:{'value':False} for name in ['depth_mirror','depth_flip','color_mirror','color_flip']}}
        # A nonidentity transform catches reapplying depth_to_color or using a transpose
        # in the wrong direction. Point geometry must return to the native depth frame.
        self.record['actual_profile_extrinsics']['depth_to_color']['translation_mm']=[10,20,30]
        write_json(self.source/'reconstruction.json',self.record)
        y,x=np.indices((7,9))
        Image.fromarray(np.stack([x*20,y*20,x*0+80],axis=-1).astype(np.uint8)).save(self.source/'color.png')
        self.codes=np.full((5,7),4001,np.uint16); self.codes[0,0]=0
        np.save(self.source/'depth_aligned_to_color_code_u16.npy',self.codes)
        np.save(self.source/'depth_aligned_to_color_mm.npy',np.rint(self.codes*.25).astype(np.uint16))
        Image.new('L',(7,5),255).save(self.seg/'trunk_mask_draft.png')
        self.refresh_metadata()

    def refresh_metadata(self):
        _,_,geometry,_=prepare_frame(self.source,'rgb')
        self.meta={'input_mode':'rgb','shape_hw':[5,7],'reviewed':False,'status':'draft',
                   'pig_id':'rgb','capture_id':'capture','revision':0,'geometry':geometry,
                   'source_sha256':geometry['source_sha256']}
        write_json(self.seg/'segmentation.json',self.meta)

    def tearDown(self): self.tmp.cleanup()

    def test_rgb_grid_uses_its_own_profile_scale_and_inverse_extrinsic_once(self):
        payload,report,mask=reconstruct(self.source,self.seg,True)
        self.assertEqual(report['coordinate_system'],'native_depth_camera')
        self.assertEqual(report['pixel_coordinate_space'],'aligned_color')
        self.assertEqual(len(payload['xyz_mm']),34)
        uv=payload['pixel_uv']; z=1000.25
        expected=np.column_stack([(uv[:,0]-3)/100*z-10,
                                   (uv[:,1]-2)/100*z-20,np.full(len(uv),z-30)])
        np.testing.assert_allclose(payload['xyz_mm'],expected,atol=5e-5)
        # Image remapping, rather than blindly indexing the native RGB grid.
        np.testing.assert_allclose(payload['rgb'][:,0],(uv[:,0]+1)*20,atol=1)
        self.assertTrue(payload['rgb_valid'].all())
        self.assertTrue(np.isnan(payload['infrared_raw']).all())
        self.assertFalse(payload['infrared_valid'].any())
        self.assertFalse(report['infrared_available'])

    def test_draft_gate_and_old_rgb_geometry_are_not_silently_bypassed(self):
        with self.assertRaisesRegex(ValueError,'尚未人工确认'):
            reconstruct(self.source,self.seg)
        self.meta.pop('geometry')
        write_json(self.seg/'segmentation.json',self.meta)
        with self.assertRaisesRegex(ValueError,'新建 RGB 任务'):
            reconstruct(self.source,self.seg,True)

    def test_native_camera_conversion_inverts_rotation_and_translation(self):
        self.record['actual_profile_extrinsics']['depth_to_color']['rotation_row_major']=[0,-1,0,1,0,0,0,0,1]
        write_json(self.source/'reconstruction.json',self.record)
        self.refresh_metadata()
        payload,report,_=reconstruct(self.source,self.seg,True)
        uv=payload['pixel_uv']; z=1000.25
        expected=np.column_stack([(uv[:,1]-2)/100*z-20,
                                   -((uv[:,0]-3)/100*z-10),np.full(len(uv),z-30)])
        np.testing.assert_allclose(payload['xyz_mm'],expected,atol=5e-5)

    def test_source_change_stale_geometry_and_mismatched_depth_are_rejected(self):
        codes=self.codes.copy(); codes[1,1]+=100
        np.save(self.source/'depth_aligned_to_color_code_u16.npy',codes)
        with self.assertRaisesRegex(ValueError,'已变化'):
            reconstruct(self.source,self.seg,True)
        self.refresh_metadata()
        with self.assertRaisesRegex(ValueError,'毫米数组不一致'):
            reconstruct(self.source,self.seg,True)
        np.save(self.source/'depth_aligned_to_color_code_u16.npy',self.codes)
        self.refresh_metadata()
        self.meta['geometry']['geometry_id']='stale'
        write_json(self.seg/'segmentation.json',self.meta)
        with self.assertRaisesRegex(ValueError,'来源已变化'):
            reconstruct(self.source,self.seg,True)

    def test_export_preserves_sources_correct_check_image_grid_and_missing_ir(self):
        before={p.name:sha256(p) for p in self.source.iterdir()}
        payload,report,mask=reconstruct(self.source,self.seg,True)
        dest=export_frame(self.root/'result',payload,report,mask)
        write_viewer(dest/'preview.html',payload,report)
        self.assertTrue((dest/'cloud_rgb_mm.ply').is_file())
        self.assertFalse((dest/'cloud_ir_mm.ply').exists())
        with Image.open(dest/'mask_depth_check.png') as image:
            self.assertEqual(image.size,(7,5))
        with Image.open(dest/'rgb_projection_check.png') as image:
            self.assertEqual(image.size,(9,7))
        with np.load(dest/'points.npz') as saved:
            np.testing.assert_array_equal(saved['xyz_mm'],payload['xyz_mm'])
            self.assertTrue(np.isnan(saved['infrared_raw']).all())
        np.testing.assert_array_equal(display_ir(payload['infrared_raw']),np.tile([110,120,140],(34,1)))
        self.assertEqual(before,{p.name:sha256(p) for p in self.source.iterdir()})

    def test_workspace_generates_rgb_without_requiring_infrared_files(self):
        source=self.root/'dataset'; seg=self.root/'segmentation'
        source.mkdir();seg.mkdir();rel='pig_rgb/capture'
        (source/'pig_rgb').mkdir();(seg/'pig_rgb').mkdir()
        shutil.copytree(self.source,source/rel);shutil.copytree(self.seg,seg/rel)
        write_json(seg/'dataset.json',{'source':str(source),'input_mode':'rgb','frames':[
            {'relative_dir':rel,'pig_id':'rgb','capture_id':'capture'}]})
        ws=Workspace(self.root/'settings')
        state=ws.load({'source':str(source),'segmentation':str(seg)})
        self.assertFalse(state['frames'][0]['missing'])
        ws.start({'dataset_id':state['dataset_id'],'ids':[state['frames'][0]['id']],
                  'parent':str(self.root),'name':'workspace-result','allow_draft':True},'generate')
        ws.worker.join(10)
        self.assertFalse(ws.worker.is_alive())
        self.assertEqual(ws.job['status'],'completed',ws.job)
        self.assertEqual(ws.state()['clouds'],1)
        self.assertEqual(Workspace(self.root/'settings').state()['clouds'],1)
        self.assertFalse((self.root/'workspace-result'/rel/'cloud_ir_mm.ply').exists())
        self.assertFalse(ws.state()['frames'][0]['has_ir_file'])
        # RGB folders from the previous version remain readable, with the old
        # placeholder identified in the export plan rather than as measured IR.
        (self.root/'workspace-result'/rel/'cloud_ir_mm.ply').write_bytes(b'legacy placeholder')
        state = ws.open_results(str(self.root/'workspace-result'))
        self.assertEqual(state['clouds'],1)
        self.assertTrue(state['frames'][0]['has_ir_file'])
        self.assertFalse(state['frames'][0]['infrared_available'])

    def test_http_rgb_preview_executes_and_rewrites_only_markup(self):
        import json
        import subprocess
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.parse import urlencode
        from urllib.request import urlopen
        from pointcloud_workbench.app import make_handler
        from pointcloud_workbench.viewer import render_viewer
        payload, report, mask = reconstruct(self.source,self.seg,True)
        dest=export_frame(self.root/'result'/'pig_rgb'/'capture',payload,report,mask)
        write_viewer(dest/'preview.html',payload,report)
        ws=Workspace(self.root/'http-settings')
        state=ws.open_results(str(self.root/'result'))
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(ws,'test-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            query=urlencode({'dataset_id':state['dataset_id'],'id':state['frames'][0]['id'],'file':'preview.html'})
            page=urlopen(f'http://127.0.0.1:{server.server_port}/api/media?'+query).read().decode()
            expected=render_viewer(payload,report,embedded=True)
            self.assertEqual(page.partition('<script>')[2],expected.partition('<script>')[2])
            self.assertIn('src="/api/media?',page.partition('<script>')[0])
            result=subprocess.run(['node',str(Path(__file__).with_name('viewer_runtime.cjs'))],
                                  input=page,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertGreater(json.loads(result.stdout)['draws'],34)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__=='__main__': unittest.main()
