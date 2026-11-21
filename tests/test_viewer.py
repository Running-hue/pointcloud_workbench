import json
import unittest

import numpy as np

from pointcloud_workbench.viewer import view_bounds, render_viewer


class ViewerFramingTest(unittest.TestCase):
    def test_boundary_far_point_does_not_shrink_body_or_change_geometry(self):
        rng = np.random.default_rng(19)
        body = rng.uniform([-400, -200, 650], [400, 200, 1100], (5000, 3))
        xyz = np.vstack([body, [-790, -560, 6500]]).astype(np.float32)
        original = xyz.copy()
        bounds = view_bounds(xyz)
        # A single distant point used to make a pig less than one sixth its size.
        self.assertGreater(np.ptp(xyz, axis=0).max(), 5800)
        self.assertLess(np.max(np.subtract(bounds['body']['max'], bounds['body']['min'])), 1000)
        self.assertEqual(bounds['outside_body_points'], 1)
        self.assertEqual(bounds['full']['max'][2], 6500)
        np.testing.assert_array_equal(xyz, original)

    def test_render_keeps_the_far_point_and_full_report_available(self):
        xyz = np.column_stack([np.linspace(-400, 400, 1000), np.zeros(1000), np.full(1000, 750)])
        xyz[-1, 2] = 6500
        payload = dict(xyz_mm=xyz, rgb=np.full((1000, 3), 100, np.uint8),
                       rgb_valid=np.ones(1000, bool), infrared_raw=np.ones(1000))
        report = {'input_mode': 'rgb', 'stats': {'xyz_max_mm': [400, 0, 6500]}}
        html = render_viewer(payload, report)
        encoded = html.partition('const DATA=')[2].partition(';\n')[0]
        data = json.loads(encoded)
        self.assertEqual(len(data['xyz']), 1000)
        self.assertEqual(data['xyz'][-1][2], 6500)
        self.assertEqual(data['report']['stats'], report['stats'])
        self.assertIn('value="full">全部点', html)


if __name__ == '__main__':
    unittest.main()
