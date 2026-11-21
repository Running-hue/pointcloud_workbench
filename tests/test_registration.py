from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pointcloud_workbench.core import np, Image, write_json
from pointcloud_workbench.registration import audit_rgb_registration, registration_image
from segmentation_workbench.tests.geometry_fixture import profile


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name)
        p = profile(100, 80, fx=100, cx=50)
        p['intrinsic'].update(fy=100, cy=40)
        # Analytic plane Z = 0.5 X + 1000, independently sampled by each camera.
        # Color is translated +100 mm in X relative to the native depth camera.
        _, x = np.indices((80, 100))
        native = 1000/(1-.5*(x-50)/100)
        aligned = 950/(1-.5*(x-50)/100)
        np.save(self.source/'depth_sensor_code_u16.npy', np.rint(native).astype(np.uint16))
        self.aligned = np.rint(aligned).astype(np.uint16)
        self.record = {'selected_capture': {'streams': {'depth': {'profile':p, 'value_scale_mm_per_code':1}}},
                       'actual_profile_extrinsics': {'depth_to_color': {'rotation_row_major':np.eye(3).ravel().tolist(),
                                                                      'translation_mm':[100, 0, 0]}}}
        distorted = deepcopy(p)
        distorted['distortion']['k1'] = .08
        self.geometry = {'depth_profile':p, 'source_profile':distorted, 'shape_hw':[80, 100]}
        self.mask = np.zeros((80, 100), np.uint8)
        self.mask[10:70, 20:80] = 255
        Image.new('RGB', (100,80), (80, 80, 80)).save(self.source/'color.png')

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_depth_samples_agree_after_calibrated_projection(self):
        audit = audit_rgb_registration(self.source, self.record, self.geometry, self.mask, self.aligned, 1)
        self.assertEqual(audit['status'], 'consistent', audit)
        self.assertGreater(audit['masked_surface']['agreement_ratio'], .99)
        self.assertFalse(audit['physical_accuracy_verified'])
        self.assertGreater(audit['native_rgb_to_mask_grid_offset_px']['max'], .1)

    def test_incorrect_extrinsic_is_reported_despite_perfect_self_reprojection(self):
        self.record['actual_profile_extrinsics']['depth_to_color']['translation_mm'] = [300, 0, 0]
        audit = audit_rgb_registration(self.source, self.record, self.geometry, self.mask, self.aligned, 1)
        self.assertEqual(audit['status'], 'review_required')
        self.assertLess(audit['masked_surface']['agreement_ratio'], .1)
        self.assertTrue(audit['warnings'])

    def test_missing_native_depth_is_explicitly_unverified(self):
        (self.source/'depth_sensor_code_u16.npy').unlink()
        audit = audit_rgb_registration(self.source, self.record, self.geometry, self.mask, self.aligned, 1)
        self.assertEqual(audit['status'], 'unavailable')
        self.assertNotIn('check_image', audit)

    def test_no_overlap_does_not_claim_consistency_or_emit_nan(self):
        self.record['actual_profile_extrinsics']['depth_to_color']['translation_mm'] = [10000, 0, 0]
        audit = audit_rgb_registration(self.source, self.record, self.geometry, self.mask, self.aligned, 1)
        self.assertEqual(audit['status'], 'review_required')
        self.assertIsNone(audit['scene']['agreement_ratio'])
        write_json(self.source/'audit.json', audit)

    def test_check_image_uses_original_rgb_grid_and_calibrated_mask_boundary(self):
        result = registration_image(self.source, self.record, self.geometry, self.mask)
        self.assertEqual(result.shape, (80, 100, 3))
        self.assertTrue(np.any(np.all(result == [255, 215, 30], axis=2)))


if __name__ == '__main__':
    unittest.main()
