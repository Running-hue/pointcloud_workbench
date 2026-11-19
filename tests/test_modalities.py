"""Camera identity and segmentation mode are independent throughout the workflow."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import unittest

from pointcloud_workbench.tests import test_geometry, test_rgb
from pointcloud_workbench.core import (read_json, write_json, sha256, np, reconstruct,
    export_frame, primary_ply, input_mode, required_result_files)
from pointcloud_workbench.workspace import Workspace
from pointcloud_workbench.viewer import render_viewer
from pointcloud_workbench.batch import run


class ModalitiesTest(unittest.TestCase):
    def test_both_cameras_and_both_modes_generate_preview_export_restore_and_cli_reuse(self):
        for model, kind in [('Orbbec Gemini 336L','stereo'), ('Orbbec Femto Bolt','itof')]:
            for mode in ['infrared','rgb']:
                with self.subTest(camera=model, mode=mode):
                    f=test_rgb.RGBCloudTest() if mode=='rgb' else test_geometry.WorkflowTest()
                    f.setUp()
                    try:
                        f.record['runtime']['device']={'name':model,'serial_number':'fixture'}
                        write_json(f.source/'reconstruction.json',f.record)
                        if mode=='rgb': f.refresh_metadata()
                        else:
                            f.meta['input_mode']=mode
                            write_json(f.seg/'segmentation.json',f.meta)
                        source,seg=f.root/'dataset',f.root/'segmentation'
                        rel='pig_test/capture'
                        (source/'pig_test').mkdir(parents=True);(seg/'pig_test').mkdir(parents=True)
                        shutil.move(f.source,source/rel);shutil.move(f.seg,seg/rel)
                        write_json(seg/'dataset.json',{'source':str(source),'input_mode':mode,'frames':[
                            {'relative_dir':rel,'source_dir':str(source/rel),'pig_id':'test','capture_id':'capture'}]})
                        before={str(p):sha256(p) for root in [source,seg] for p in root.rglob('*') if p.is_file()}
                        ws=Workspace(f.root/'settings')
                        state=ws.load({'source':str(source),'segmentation':str(seg)})
                        request={'dataset_id':state['dataset_id'],'ids':[state['frames'][0]['id']],
                                 'parent':str(f.root),'name':'generated','allow_draft':True}
                        ws.start(request,'generate');ws.worker.join(10)
                        self.assertEqual(ws.job['status'],'completed',ws.job)
                        result=f.root/'generated'/rel
                        report=read_json(result/'pointcloud.json')
                        self.assertEqual(report['camera']['type'],kind)
                        self.assertEqual(report['camera']['model'],model)
                        self.assertEqual(report['input_mode'],mode)
                        self.assertEqual({p.name for p in result.glob('*.ply')},{primary_ply(report)})
                        with np.load(result/'points.npz') as payload:
                            page=render_viewer(payload,report)
                            expected=payload['xyz_mm'].copy()
                        runtime=subprocess.run(['node',str(Path(__file__).with_name('viewer_runtime.cjs'))],
                                               input=page,text=True,capture_output=True,timeout=10)
                        self.assertEqual(runtime.returncode,0,runtime.stderr)
                        state=Workspace(f.root/'settings').state()
                        self.assertEqual(state['clouds'],1)
                        self.assertEqual(state['frames'][0]['cloud_input_mode'],mode)
                        ws.start({**request,'name':'copied'},'export');ws.worker.join(10)
                        self.assertEqual(ws.job['status'],'completed',ws.job)
                        copied=f.root/'copied'/rel
                        for name in required_result_files(report):
                            self.assertEqual(sha256(result/name),sha256(copied/name))
                        args=Namespace(segmentation=seg,output=f.root/'cli',pig=None,capture=None,limit=None,
                                       allow_draft=True,max_rgb_delta_us=10000,visibility_mm=20)
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(run(args),0)
                            self.assertEqual(run(args),0)
                        summary=read_json(args.output/'summary.json')
                        self.assertEqual(summary['existing'],[rel])
                        with np.load(args.output/rel/'points.npz') as payload:
                            np.testing.assert_array_equal(payload['xyz_mm'],expected)
                        self.assertEqual(before,{str(p):sha256(p) for root in [source,seg] for p in root.rglob('*') if p.is_file()})
                    finally: f.tearDown()

    def test_export_old_dual_ply_ir_folder_normalizes_only_the_copy(self):
        f=test_geometry.WorkflowTest();f.setUp()
        try:
            f.record['runtime']['device']={'name':'Orbbec Gemini 336L'}
            write_json(f.source/'reconstruction.json',f.record)
            payload,report,mask=reconstruct(f.source,f.seg,True)
            result=export_frame(f.root/'old'/'pig_test'/'capture',payload,report,mask)
            report.pop('input_mode');report.pop('export_policy');report.pop('primary_ply')
            report.pop('camera')
            write_json(result/'pointcloud.json',report)
            (result/'cloud_rgb_mm.ply').write_bytes(b'old secondary RGB colorization')
            (result/'preview.html').write_text('old preview')
            original={p.name:sha256(p) for p in result.iterdir()}
            ws=Workspace(f.root/'settings');state=ws.open_results(str(f.root/'old'))
            ws.start({'dataset_id':state['dataset_id'],'ids':[state['frames'][0]['id']],
                      'parent':str(f.root),'name':'normalized'},'export');ws.worker.join(10)
            self.assertEqual(ws.job['status'],'completed',ws.job)
            new=f.root/'normalized'/'pig_test'/'capture'
            self.assertEqual({p.name for p in new.glob('*.ply')},{'cloud_ir_mm.ply'})
            for name in ['cloud_ir_mm.ply','points.npz']:
                self.assertEqual(sha256(new/name),original[name])
            self.assertEqual(input_mode(read_json(new/'pointcloud.json')),'infrared')
            self.assertEqual(read_json(new/'pointcloud.json')['camera']['type'],'stereo')
            self.assertIn('IR PLY',(new/'preview.html').read_text())
            self.assertEqual(original,{p.name:sha256(p) for p in result.iterdir()})
        finally:f.tearDown()

    def test_cli_skips_rejected_capture_even_when_drafts_are_allowed(self):
        f=test_geometry.WorkflowTest();f.setUp()
        try:
            rel='pig_test/capture';source=f.root/'dataset';seg=f.root/'segmentation'
            (source/'pig_test').mkdir(parents=True);(seg/'pig_test').mkdir(parents=True)
            shutil.move(f.source,source/rel);shutil.move(f.seg,seg/rel)
            f.meta['decision']='reject';write_json(seg/rel/'segmentation.json',f.meta)
            write_json(seg/'dataset.json',{'source':str(source),'frames':[
                {'relative_dir':rel,'source_dir':str(source/rel),'pig_id':'test','capture_id':'capture'}]})
            args=Namespace(segmentation=seg,output=f.root/'cli',pig=None,capture=None,limit=None,
                           allow_draft=True,max_rgb_delta_us=10000,visibility_mm=20)
            with redirect_stdout(io.StringIO()):self.assertEqual(run(args),0)
            self.assertFalse((args.output/rel).exists())
            self.assertEqual(read_json(args.output/'summary.json')['skipped'][0]['reason'],'segmentation_rejected')
        finally:f.tearDown()
