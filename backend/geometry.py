"""Revalidate segmentation coordinates without requiring the segmentation app.

The version fields, geometry identity and RGB remapping follow the geometry
contract used by pig-segmentation-filter/geometry.py. Keep the identity JSON
compatible with existing segmentation results; never bypass source validation.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

from .core import Image, np, cv2


def read_frame(folder):
    """Match the segmentation IR input and validate its depth/preview grid."""
    ir = np.load(folder/'infrared.npy', allow_pickle=False)
    depth = np.load(folder/'depth_mm.npy', allow_pickle=False)
    if ir.ndim != 2 or depth.shape != ir.shape:
        raise ValueError('红外与原始深度数组必须是同尺寸二维数组')
    with Image.open(folder/'infrared.png') as preview:
        if preview.size != (ir.shape[1], ir.shape[0]):
            raise ValueError('红外 PNG 与 NPY 尺寸不一致')
    valid = np.isfinite(ir) & (ir > 0)
    if not valid.any():
        raise ValueError('红外数据没有有效像素')
    lo, hi = np.percentile(ir[valid], [1, 99.5])
    if hi <= lo:
        raise ValueError('红外图缺少可分割的亮度变化')
    normalized = np.clip((ir.astype(np.float32)-lo)/(hi-lo), 0, 1)
    return (np.power(normalized, .65)*255).astype(np.uint8), depth


RGB_VERSION = 'rgb-aligned-depth-v1'
RGB_LEGACY = 'rgb-native-unverified-v0'
IR_VERSION = 'infrared-depth-native-v1'
COEFFICIENTS = ('k1','k2','p1','p2','k3','k4','k5','k6')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def camera(profile):
    """Validate the actual frame profile; OpenCV rational Brown coefficient order."""
    try:
        intrinsic = profile['intrinsic']
        fx,fy,cx,cy = [float(intrinsic[k]) for k in ('fx','fy','cx','cy')]
        w,h = [int(intrinsic[k]) for k in ('width','height')]
        if not np.isfinite([fx,fy,cx,cy]).all() or min(fx,fy,w,h)<=0:
            raise ValueError('内参数值无效')
        if (profile.get('width',w),profile.get('height',h)) != (w,h):
            raise ValueError('流分辨率与内参分辨率不一致')
        distortion = profile['distortion']
        coefficients = np.array([float(distortion.get(k,0)) for k in COEFFICIENTS],np.float64)
        if not np.isfinite(coefficients).all():
            raise ValueError('畸变系数无效')
        model=distortion.get('model_name')
        if model not in {'OB_DISTORTION_BROWN_CONRADY','OB_DISTORTION_BROWN_CONRADY_K6','OB_DISTORTION_NONE'} or (model=='OB_DISTORTION_NONE' and coefficients.any()):
            raise ValueError('不支持该畸变模型，不能当成 Brown 模型处理')
        return np.array([[fx,0,cx],[0,fy,cy],[0,0,1]],np.float64), coefficients, (w,h)
    except (KeyError,TypeError,OverflowError) as exc:
        raise ValueError('实际相机 profile 缺少完整内参或畸变信息') from exc


def same_camera(a,b):
    ka,da,sa=camera(a);kb,db,sb=camera(b)
    return sa==sb and np.allclose(ka,kb,rtol=0,atol=1e-8) and np.allclose(da,db,rtol=0,atol=1e-10)


def normalized_rays(pixels,matrix,distortion):
    pixels=np.asarray(pixels,np.float64).reshape(-1,1,2)
    if not np.any(distortion):
        return (pixels.reshape(-1,2)-matrix[:2,2])/np.array([matrix[0,0],matrix[1,1]])
    criteria=(cv2.TERM_CRITERIA_COUNT|cv2.TERM_CRITERIA_EPS,50,1e-10)
    # OpenCV 4 exposes Iter separately; OpenCV 5 merges criteria into the main API.
    if hasattr(cv2,'undistortPointsIter'):
        result=cv2.undistortPointsIter(pixels,matrix,distortion,None,None,criteria)
    else:
        result=cv2.undistortPoints(pixels,matrix,distortion,criteria=criteria)
    return result.reshape(-1,2)


def profiles(folder,mode):
    """Do not substitute session-start calibration for the saved output profile."""
    folder=Path(folder)
    path=folder/'reconstruction.json'
    if not path.is_file():
        raise ValueError('缺少 reconstruction.json，无法确认所保存深度的实际像素坐标；请保留原采集的重建记录')
    data=json.loads(path.read_text(encoding='utf-8'))
    try:
        streams=data['selected_capture']['streams']
        if mode=='rgb':
            align=data['alignment']
            if align.get('operation')!='Orbbec_software_Align_to_COLOR':
                raise ValueError('无法确认已保存深度对齐到彩色相机，不能仅凭文件名使用')
            source=streams['color']['profile']
            target=align['output_depth_frame']['profile']
            frame=align['output_depth_frame']
            if not align.get('target_color_identity') or not align.get('source_depth_identity') or align['target_color_identity']!=streams['color'].get('identity') or align['source_depth_identity']!=streams['depth'].get('identity'):
                raise ValueError('对齐记录与所选 RGB／深度帧身份不一致，无法确认配对关系')
        else:
            source=streams['infrared']['profile']
            target=streams['depth']['profile']
            frame=streams['depth']
            if not same_camera(source,target):
                raise ValueError('红外与原始深度的实际内参／畸变不同，不能直接逐像素套用掩码')
            extrinsics=data['actual_profile_extrinsics']
            for name in ['infrared_to_color','depth_to_color']:
                for key,length in [('rotation_row_major',9),('translation_mm',3)]:
                    values=np.asarray(extrinsics[name][key],np.float64)
                    if values.shape!=(length,) or not np.isfinite(values).all():
                        raise ValueError('实际相机外参尺寸或数值无效')
            if not all(np.allclose(extrinsics['infrared_to_color'][k],extrinsics['depth_to_color'][k],rtol=0,atol=1e-8)
                       for k in ['rotation_row_major','translation_mm']):
                raise ValueError('红外与深度不在同一相机坐标，尚不支持该采集的直接掩码反投影')
        camera(source);camera(target)
        scale=float(frame['value_scale_mm_per_code'])
        if not np.isfinite(scale) or scale<=0:
            raise ValueError('记录的深度单位比例无效')
    except (KeyError,TypeError) as exc:
        raise ValueError('重建记录缺少实际流 profile、外参或深度单位，无法核对几何关系') from exc
    return source,target,scale,data


def calibration_metadata(folder,mode):
    folder=Path(folder)
    source,target,scale,data=profiles(folder,mode)
    matrix,coefficients,size=camera(target)
    depth_file='depth_aligned_to_color_mm.npy' if mode=='rgb' else 'depth_mm.npy'
    code_file='depth_aligned_to_color_code_u16.npy' if mode=='rgb' else 'depth_sensor_code_u16.npy'
    sources={'reconstruction.json':digest(folder/'reconstruction.json')}
    if (folder/'calibration.json').is_file():sources['calibration.json']=digest(folder/'calibration.json')
    for name in [depth_file,code_file,'color.png' if mode=='rgb' else 'infrared.npy']:
        if (folder/name).is_file():sources[name]=digest(folder/name)
    warnings=[]
    if not data.get('selected_capture',{}).get('infrared_same_frameset',True) and mode=='infrared':
        warnings.append('红外与深度不是同一 FrameSet，需核查运动造成的时序误差')
    has_depth=(folder/depth_file).is_file()
    if not has_depth:warnings.append('缺少对应深度数组，暂不能生成点云')
    return {'schema_version':1,'version':RGB_VERSION if mode=='rgb' else IR_VERSION,
            'input_mode':mode,'status':'verified','coordinate_space':'aligned_color' if mode=='rgb' else 'depth_native',
            'camera_frame':'color' if mode=='rgb' else 'depth','shape_hw':[size[1],size[0]],
            'source_image_file':'color.png' if mode=='rgb' else 'infrared.npy',
            'image_transform':('identity_profile_match' if same_camera(source,target) else 'remap_rgb_to_saved_aligned_depth') if mode=='rgb' else 'infrared_native',
            'source_profile':{'intrinsic':source['intrinsic'],'distortion':source['distortion']},
            'depth_profile':{'intrinsic':target['intrinsic'],'distortion':target['distortion']},
            'camera_matrix':matrix.tolist(),'distortion_coefficients_opencv':coefficients.tolist(),
            'depth_file':depth_file if has_depth else None,
            'depth_code_file':code_file if (folder/code_file).is_file() else None,
            'depth_scale_mm_per_code':scale,'length_unit':'mm','depth_value_semantics':'camera_z',
            'extrinsics_applied_to_saved_aligned_depth':False,
            'unprojection':'undistort_pixels_using_depth_profile_then_xyz_equals_ray_times_depth_z',
            'source_sha256':sources,'pointcloud_ready':has_depth,'warnings':warnings,
            'valid_region_file':'image_valid_mask.png'}


@lru_cache(maxsize=4)
def remap_grid(source_json,target_json):
    source=json.loads(source_json);target=json.loads(target_json)
    ks,ds,(sw,sh)=camera(source);kt,dt,(w,h)=camera(target)
    if not dt.any():
        map_x,map_y=cv2.initUndistortRectifyMap(ks,ds,np.eye(3),kt,(w,h),cv2.CV_32FC1)
    else:
        # A target with distortion requires its own inverse model before source projection.
        yy,xx=np.indices((h,w),dtype=np.float32)
        pixels=np.stack([xx,yy],axis=-1).reshape(-1,1,2)
        rays=normalized_rays(pixels,kt,dt)
        projected,_=cv2.projectPoints(np.column_stack([rays,np.ones(len(rays))]),np.zeros(3),np.zeros(3),ks,ds)
        mapped=projected.reshape(h,w,2)
        map_x,map_y=mapped[...,0].astype(np.float32),mapped[...,1].astype(np.float32)
    # Validity includes the bilinear sampling footprint, not the presence of depth.
    valid=np.isfinite(map_x)&np.isfinite(map_y)&(map_x>=0)&(map_y>=0)&(map_x<=sw-1)&(map_y<=sh-1)
    map_x.setflags(write=False);map_y.setflags(write=False);valid.setflags(write=False)
    return map_x,map_y,valid


def unverified_geometry(mode,shape,reason):
    return {'schema_version':1,'version':RGB_LEGACY if mode=='rgb' else IR_VERSION,
            'input_mode':mode,'status':'unverified','coordinate_space':'color_native' if mode=='rgb' else 'infrared_native',
            'shape_hw':list(shape),'pointcloud_ready':False,'depth_file':None,
            'warnings':[reason],'valid_region_file':'image_valid_mask.png'}


def prepare_frame(folder,mode='infrared',version=None):
    """Return the exact image, depth and validity grid to use throughout the editor."""
    folder=Path(folder)
    if mode=='infrared':
        image,depth=read_frame(folder)
        try:
            geometry=calibration_metadata(folder,mode)
        except ValueError as exc:
            if (folder/'reconstruction.json').is_file():raise
            geometry=unverified_geometry(mode,image.shape,str(exc))
        if geometry['status']=='verified' and geometry['shape_hw']!=list(image.shape):
            raise ValueError('红外／深度数组尺寸与实际 profile 不符')
        valid=np.ones(image.shape,bool)
    else:
        with Image.open(folder/'color.png') as im:image=np.asarray(im.convert('RGB')).copy()
        if version==RGB_LEGACY:
            reason='旧 RGB 成果未做几何校正，仅供查看；请新建 RGB 任务后再用于点云'
            geometry=unverified_geometry(mode,image.shape[:2],reason)
            return image,None,geometry,np.ones(image.shape[:2],bool)
        if version not in {None,RGB_VERSION}:raise ValueError('未知的 RGB 几何版本')
        geometry=calibration_metadata(folder,mode)
        source=geometry['source_profile'];target=geometry['depth_profile']
        if camera(source)[2]!=(image.shape[1],image.shape[0]):
            raise ValueError('原始 RGB 尺寸与实际彩色流 profile 不符')
        if geometry['image_transform']=='identity_profile_match':
            valid=np.ones(image.shape[:2],bool)
        else:
            map_x,map_y,valid=remap_grid(json.dumps(source,sort_keys=True),json.dumps(target,sort_keys=True))
            image=cv2.remap(image,map_x,map_y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=0)
        depth=np.load(folder/geometry['depth_file'],allow_pickle=False) if geometry['depth_file'] else None
        if depth is not None and (depth.ndim!=2 or list(depth.shape)!=geometry['shape_hw']):
            raise ValueError('对齐 RGB 的深度数组必须与其实际输出 profile 尺寸一致')
    geometry['valid_image_ratio']=float(valid.mean())
    identity={k:v for k,v in geometry.items() if k not in {'valid_image_ratio','geometry_id'}}
    geometry['geometry_id']=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return image,depth,geometry,valid


def validate_mask_region(mask,valid):
    if mask.shape!=valid.shape:raise ValueError('掩码与几何目标网格尺寸不一致')
    if np.any((mask>0)&~valid):
        raise ValueError('掩码包含 RGB 重映射后的无效边缘，请擦除黑边区域再保存')

