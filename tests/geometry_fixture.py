"""Known synthetic calibration independent of a developer's camera/data paths."""
import json


def profile(width,height,distortion=None,fx=None,cx=None):
    d={'model':4,'model_name':'OB_DISTORTION_BROWN_CONRADY_K6',
       **{k:0. for k in ['k1','k2','k3','k4','k5','k6','p1','p2']}}
    d.update(distortion or {})
    return {'width':width,'height':height,'intrinsic':{'width':width,'height':height,
            'fx':fx or width*.8,'fy':height*.9,'cx':width/2 if cx is None else cx,'cy':height/2},
            'distortion':d}


def calibration(folder,rgb_size=(180,120),ir_size=(100,80),source=None,target=None,scale=1.):
    color=source or profile(*rgb_size)
    aligned=target or profile(*rgb_size)
    depth=profile(*ir_size)
    identity={'rotation_row_major':[1,0,0,0,1,0,0,0,1],'translation_mm':[0,0,0]}
    record={'selected_capture':{'infrared_same_frameset':True,'streams':{
                'color':{'profile':color,'identity':{'sdk_frame_index':'11'}},
                'depth':{'profile':depth,'identity':{'sdk_frame_index':'12'},'value_scale_mm_per_code':scale},
                'infrared':{'profile':depth}}},
            'alignment':{'operation':'Orbbec_software_Align_to_COLOR',
                         'output_depth_frame':{'profile':aligned,'value_scale_mm_per_code':scale},
                         'target_color_identity':{'sdk_frame_index':'11'},
                         'source_depth_identity':{'sdk_frame_index':'12'}},
            'actual_profile_extrinsics':{'depth_to_color':identity,'infrared_to_color':identity}}
    (folder/'reconstruction.json').write_text(json.dumps(record), encoding='utf-8')
    return record
