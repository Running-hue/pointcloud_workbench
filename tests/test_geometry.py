from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest

from pointcloud_workbench.core import (backproject, camera, project, sample_rgb, reconstruct,
    write_json, sha256, choose_mask, independent_paths, within, export_frame, np, cv2, Image)


def profile(width=7, height=5):
    return {'intrinsic': {'fx': 100., 'fy': 100., 'cx': 3., 'cy': 2.,
                          'width': width, 'height': height},
            'distortion': {'model_name': 'OB_DISTORTION_BROWN_CONRADY_K6',
                           **dict.fromkeys(['k1','k2','k3','k4','k5','k6','p1','p2'], 0.)}}


def extrinsic():
    return {'rotation_row_major': np.eye(3).ravel().tolist(), 'translation_mm': [0, 0, 0]}


class GeometryTest(unittest.TestCase):
    def test_real_dimensions_and_fractional_depth_scale(self):
        codes = np.full((5, 7), 2000, dtype=np.uint16)
        codes[0, 0] = 0
        xyz, valid, error = backproject(codes, .5, profile())
        np.testing.assert_allclose(xyz[2, 3], [0, 0, 1000])
        np.testing.assert_allclose(xyz[2, 5]-xyz[2, 1], [40, 0, 0])
        self.assertFalse(valid[0, 0])
        self.assertEqual(valid.sum(), 34)
        self.assertLess(error[valid].max(), 1e-8)

    def test_strong_distortion_inversion_and_independent_projection(self):
        p = profile(80, 60)
        p['intrinsic'].update(fx=65., fy=65., cx=40., cy=30.)
        p['distortion'].update(k1=22., k2=10., k3=.3, k4=22.3, k5=17.8, k6=2.25, p1=.00002, p2=.00003)
        xyz, valid, error = backproject(np.full((60, 80), 750, np.uint16), 1, p)
        self.assertTrue(valid.all())
        # OpenCV forward projection is independent of our explicit forward formula.
        k, d = camera(p)
        uv, _ = cv2.projectPoints(xyz.reshape(-1, 3), np.zeros(3), np.zeros(3), k, d)
        v, u = np.indices((60, 80))
        expected = np.column_stack([u.ravel(), v.ravel()])
        np.testing.assert_allclose(uv.reshape(-1, 2), expected, atol=1e-6)
        naive_x = (u-p['intrinsic']['cx'])/p['intrinsic']['fx']*750
        self.assertGreater(abs(naive_x[0, 0]-xyz[0, 0, 0]), 10)

    def test_occluded_rgb_is_missing_without_deleting_geometry(self):
        p = profile()
        scene = np.array([[[0.,0.,500.], [0.,0.,1000.], [1000.,0.,1000.]]])
        valid = np.ones((1, 3), bool)
        image = np.zeros((5, 7, 3), np.uint8)
        image[2, 3] = [210, 45, 80]
        rgb, good, uv, _ = sample_rgb(scene, valid, np.array([[0,0],[0,1],[0,2]]), image, p, extrinsic())
        np.testing.assert_array_equal(good, [True, False, False])
        np.testing.assert_array_equal(rgb[0], [210,45,80])
        self.assertEqual(len(rgb), 3)

    def test_color_extrinsic_applied_once(self):
        p = profile()
        scene = np.array([[[0.,0.,1000.]]])
        image = np.zeros((5, 7, 3), np.uint8)
        image[2, 4] = [5, 100, 220]
        e = extrinsic()
        e['translation_mm'] = [10., 0., 0.]
        rgb, good, uv, _ = sample_rgb(scene, np.ones((1,1), bool), np.array([[0,0]]), image, p, e)
        np.testing.assert_allclose(uv, [[4, 2]])
        np.testing.assert_array_equal(rgb[0], [5,100,220])
        np.testing.assert_array_equal(scene, [[[0.,0.,1000.]]])


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source, self.seg = self.root/'source', self.root/'seg'
        self.source.mkdir(); self.seg.mkdir()
        codes = np.full((5,7), 2000, np.uint16)
        codes[0,0] = 0
        for name, array in [('depth_sensor_code_u16.npy', codes), ('depth_mm.npy', codes//2),
                            ('infrared.npy', np.arange(35, dtype=np.uint16).reshape(5,7)+20)]:
            np.save(self.source/name, array)
        Image.fromarray(np.ones((5,7), np.uint16)*100).save(self.source/'infrared.png')
        Image.new('RGB',(7,5),(30,80,160)).save(self.source/'color.png')
        self.record = {'selected_capture': {'streams': {}, 'timestamp_deltas': {'color_minus_depth_device_us': '0'}},
                       'actual_profile_extrinsics': {'depth_to_color': extrinsic(), 'infrared_to_color': extrinsic()},
                       'runtime': {'device_properties_at_session_start': {
                           k: {'value': False} for k in ['depth_mirror','infrared_mirror','depth_flip','infrared_flip','color_mirror','color_flip']}}}
        for name in ['depth', 'infrared', 'color']:
            self.record['selected_capture']['streams'][name] = {
                'profile': profile(), 'identity': {'present': True, 'sdk_frame_index': '1', 'device_timestamp_us': '1000'},
                'value_scale_mm_per_code': .5}
        write_json(self.source/'reconstruction.json', self.record)
        self.meta = {'reviewed': False, 'status': 'draft', 'revision': 0, 'pig_id': 'test', 'capture_id': 'capture',
                     'source_sha256': {n:sha256(self.source/n) for n in ['infrared.npy','infrared.png','depth_mm.npy']}}
        write_json(self.seg/'segmentation.json', self.meta)
        Image.new('L',(7,5),255).save(self.seg/'trunk_mask_draft.png')

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_rejects_draft_and_explicit_preview_exports_mm(self):
        with self.assertRaisesRegex(ValueError, '尚未人工确认'):
            reconstruct(self.source, self.seg)
        before = {p.name:sha256(p) for p in self.source.iterdir()}
        payload, report, mask = reconstruct(self.source, self.seg, True)
        self.assertEqual(len(payload['xyz_mm']),34)
        self.assertFalse(report['reviewed'])
        self.assertEqual(report['unit'], 'mm')
        self.assertTrue(np.all(payload['xyz_mm'][:,2] == 1000))
        self.assertAlmostEqual(report['stats']['valid_depth_ratio'],34/35)
        dest = export_frame(self.root/'output', payload, report, mask)
        with np.load(dest/'points.npz') as saved:
            np.testing.assert_array_equal(saved['xyz_mm'],payload['xyz_mm'])
        self.assertFalse((dest/'cloud_rgb_mm.ply').exists())
        self.assertEqual(report['input_mode'], 'infrared')
        self.assertTrue(payload['infrared_valid'].all())
        raw = (dest/'cloud_ir_mm.ply').read_bytes()
        header, body = raw.split(b'end_header\n')
        self.assertIn(b'unit mm', header)
        self.assertIn(b'element vertex 34', header)
        self.assertEqual(len(body), 34*28)
        self.assertEqual(before, {p.name:sha256(p) for p in self.source.iterdir()})

    def test_reviewed_mask_can_enter_normal_workflow(self):
        self.meta.update(reviewed=True,status='reviewed')
        write_json(self.seg/'segmentation.json',self.meta)
        Image.new('L',(7,5),255).save(self.seg/'trunk_mask.png')
        payload, report, _ = reconstruct(self.source,self.seg)
        self.assertTrue(report['reviewed'])

    def test_unsynchronized_rgb_preserves_every_geometry_point(self):
        self.record['selected_capture']['timestamp_deltas']['color_minus_depth_device_us']='25000'
        write_json(self.source/'reconstruction.json',self.record)
        payload, report, _ = reconstruct(self.source,self.seg,True)
        self.assertEqual(len(payload['xyz_mm']),34)
        self.assertFalse(payload['rgb_valid'].any())
        self.assertFalse(report['rgb']['enabled'])

    def test_changed_inputs_and_ir_registration_rejected(self):
        np.save(self.source/'infrared.npy', np.ones((5,7),np.uint16))
        with self.assertRaisesRegex(ValueError,'源数据已变化'):
            reconstruct(self.source,self.seg,True)
        self.meta['source_sha256']['infrared.npy']=sha256(self.source/'infrared.npy')
        write_json(self.seg/'segmentation.json',self.meta)
        self.record['selected_capture']['streams']['infrared']['identity']['sdk_frame_index']='2'
        write_json(self.source/'reconstruction.json',self.record)
        with self.assertRaisesRegex(ValueError,'同一采集帧'):
            reconstruct(self.source,self.seg,True)

    def test_path_protection(self):
        with self.assertRaises(ValueError): independent_paths(self.source,self.source/'output')
        with self.assertRaises(ValueError): within(self.source,'../escape')
        with self.assertRaises(ValueError): within(self.source,'/tmp')


if __name__ == '__main__':
    unittest.main()
