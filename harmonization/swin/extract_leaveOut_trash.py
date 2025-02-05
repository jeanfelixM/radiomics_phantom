import sys
sys.path.append('/home/reza/radiomics_phantom/')

import csv
import os
import shutil
import tempfile
from glob import glob

import time
import nibabel as nib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from tqdm import tqdm
from utils import load_data,get_model,get_model_oscar
from monai.data import Dataset, DataLoader,SmartCacheDataset
from monai.losses import DiceCELoss
from monai.inferers import sliding_window_inference
from monai.transforms.utils import generate_spatial_bounding_box, compute_divisible_spatial_size,convert_data_type
from monai.transforms.transform import LazyTransform, MapTransform
from monai.utils import ensure_tuple,convert_to_tensor
import threading
from monai.transforms.croppad.array import Crop
from torch.utils.data._utils.collate import default_collate
from monai.transforms import (
    ScaleIntensityd,
    AsDiscrete,
    Compose,
    CropForegroundd,
    LoadImaged,
    Orientationd,
    RandFlipd,
    RandCropByPosNegLabeld,
    RandShiftIntensityd,
    ScaleIntensityRanged,
    Spacingd,
    RandRotate90d,
    EnsureTyped,
    Resized, 
    ToTensord,Orientationd, 
    MaskIntensityd,
    Transform,
    EnsureChannelFirstd,
    AsDiscreted,
)

from monai.config import print_config
from monai.metrics import DiceMetric
from monai.networks.nets import SwinUNETR

from monai.data import (
    ThreadDataLoader,
    CacheDataset,
    load_decathlon_datalist,
    decollate_batch,
    set_track_meta,
)

from qa4iqi_extraction.constants import (
    SERIES_NUMBER_FIELD,
    SERIES_DESCRIPTION_FIELD,
    MANUFACTURER_MODEL_NAME_FIELD,
    MANUFACTURER_FIELD,
    SLICE_SPACING_FIELD,
    SLICE_THICKNESS_FIELD,
)


import torch






def filter_none(data, default_spacing=1.0):
    """Recursively filter out None values in the data and provide defaults for missing keys."""
    if isinstance(data, dict):
        filtered = {k: filter_none(v, default_spacing) for k, v in data.items() if v is not None}
        filtered['SpacingBetweenSlices'] = torch.tensor([default_spacing])
        return filtered
    elif isinstance(data, list):
        return [filter_none(item, default_spacing) for item in data if item is not None]
    return data

def custom_collate_fn(batch, default_spacing=1.0):
    filtered_batch = [filter_none(item, default_spacing) for item in batch]
    if not filtered_batch or all(item is None for item in filtered_batch):
        raise ValueError("Batch is empty after filtering out None values.")

    # Remove the ROI from the data to be collated, since it's not needed after image resizing
    for item in filtered_batch:
        if 'roi' in item:
            del item['roi']  

    try:
        return torch.utils.data.dataloader.default_collate(filtered_batch)
    except Exception as e:
        raise RuntimeError(f"Failed to collate batch: {str(e)}")


class DebugTransform(Transform):
    def __call__(self, data):
        print(f"Image shape: {data['image'].shape}, Mask shape: {data['roi'].shape}")
        print(f"Unique values in mask: {np.unique(data['roi'])}")
        print(f"Sum of image pixel values: {data['image'].sum()}")
        return data

class CropOnROI(Crop):
    def compute_center(self, img: torch.Tensor):
        """
        Compute the start points and end points of bounding box to crop.
        And adjust bounding box coords to be divisible by `k`.

        """
        
        def is_positive(x):
            return torch.gt(x, 0)
        box_start, box_end = generate_spatial_bounding_box(
            img, is_positive, None, 0, True
        )
        box_start_, *_ = convert_data_type(box_start, output_type=np.ndarray, dtype=np.int16, wrap_sequence=True)
        box_end_, *_ = convert_data_type(box_end, output_type=np.ndarray, dtype=np.int16, wrap_sequence=True)
        orig_spatial_size = box_end_ - box_start_
        # make the spatial size divisible by `k`
        spatial_size = np.asarray(compute_divisible_spatial_size(orig_spatial_size.tolist(), k=1))
        # update box_start and box_end
        box_start_ = box_start_ - np.floor_divide(np.asarray(spatial_size) - orig_spatial_size, 2)
        box_end_ = box_start_ + spatial_size
        print("BOX START",box_start_)
        print("BOX END",box_end_)
        #print("bouding box size",spatial_size)
        #self.write_box_start(box_start_)
        
        mid_point = np.floor((box_start_ + box_end_) / 2)
        #print("MID POINT",mid_point)
        return mid_point
    
    def write_box_start(self, box_start):
        with self.lock:
            with open(self.output_file, 'a') as f:
                f.write(f"{box_start[0]},{box_start[1]},{box_start[2]}\n")

    
    def __init__(self, roi,size, lazy=False,precomputed=False):
        super().__init__(lazy)
        self.output_file = "boxpos.txt"
        self.lock = threading.Lock()
        if precomputed:
            center = roi
        else:
            center = self.compute_center(roi)
        
        self.slices = self.compute_slices(
            roi_center=center, roi_size=size, roi_start=None, roi_end=None, roi_slices=None
        )
    def __call__(self, img: torch.Tensor, lazy = None):
        lazy_ = self.lazy if lazy is None else lazy
        return super().__call__(img=img, slices=ensure_tuple(self.slices), lazy=lazy_)
        
class CropOnROId(MapTransform, LazyTransform):
    backend = Crop.backend

    def __init__(self, keys,roi_key,size, allow_missing_keys: bool = False, lazy: bool = False,id_key="id",precomputed= False,centers=None):
        MapTransform.__init__(self, keys, allow_missing_keys)
        LazyTransform.__init__(self, lazy)
        self.id_key = id_key
        self.roi_key = roi_key
        self.size = size
        self.precomputed = precomputed
        self.centers = centers

    @LazyTransform.lazy.setter  # type: ignore
    def lazy(self, value: bool) -> None:
        self._lazy = value
        if isinstance(self.cropper, LazyTransform):
            self.cropper.lazy = value


    def __call__(self, data, lazy= None):
        d = dict(data)
        lazy_ = self.lazy if lazy is None else lazy
        #print("LA SHAPE DE SIZE",(torch.tensor(self.size)).shape)
        for key in self.key_iterator(d):
            #print("KEY",key)
            if self.precomputed:
                d[key] = CropOnROI(self.centers[d[self.roi_key]],size=self.size,lazy=lazy_,precomputed=self.precomputed)(d[key])
            else:
                d[key] = CropOnROI(d[self.roi_key],size=self.size,lazy=lazy_)(d[key])
            #d[self.id_key] = d['roi_label']
        return d

class CopyPathd(MapTransform):
    def __init__(self, keys, allow_missing_keys=False):
        super().__init__(keys, allow_missing_keys)

    def __call__(self, data):
        for key in self.keys:
            data[f"{key}_path"] = data[key]  # Copier le chemin du fichier dans une nouvelle clé
        return data
centersrois = {'cyst1':  [324, 334, 158],'cyst2' :  [189, 278, 185],'hemangioma' :  [209, 315, 159],'metastasis' : [111, 271, 140],'normal1' : [161, 287, 149],'normal2' :  [154, 229, 169]}
#./dataset_info_full_uncompressed_NAS.json
def run_inference(jsonpath = "./expanded_registered_light_dataset_info.json",fnames = ""):
    
    fname = os.path.basename(fnames[0]).split('_0')[0]
    current_fname = None
    scanner_labels = ['A1', 'A2', 'B1', 'B2', 'C1', 'D1', 'E1', 'E2', 'F1', 'G1', 'G2', 'H1', 'H2']
    device_id = 0
    os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
    torch.cuda.set_device(device_id)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu") 
    
    
    print_config()
    target_size = (64, 64, 32)
    transforms = Compose([
        LoadImaged(keys=["image"], ensure_channel_first=True),
        #DebugTransform(),
        CropOnROId(keys=["image"],roi_key="roi_label",size=target_size,precomputed=True,centers=centersrois),
        # ScaleIntensityd(keys=["image"],minv=0.0, maxv=1.0),
        # Spacingd(
        #     keys=["image"],
        #     pixdim=(1.5, 1.5, 2.0),
        #     mode=("bilinear"),
        # ),
        EnsureTyped(keys=["image"], device=device, track_meta=False),
        
        #ToTensord(keys=["image"]),
    ])

    datafiles = load_data(jsonpath)#[0::100]
    #dataset = SmartCacheDataset(data=datafiles, transform=transforms, cache_rate=0.009, progress=True, num_init_workers=8, num_replace_workers=8)
    dataset = SmartCacheDataset(data=datafiles, transform=transforms,cache_rate=1,progress=True,num_init_workers=8, num_replace_workers=8,replace_rate=0.1)
    print("dataset length: ", len(datafiles))
    dataload = ThreadDataLoader(dataset, batch_size=1, collate_fn=custom_collate_fn)
    #qq chose comme testload = DataLoader(da.....
    slice_num = 15
    with open(f"features_{os.path.basename(fname)}.csv", "w", newline="") as csvfile:
        fieldnames = ["SeriesNumber", "deepfeatures", "ROI", "SeriesDescription", "ManufacturerModelName", "Manufacturer", "SliceThickness"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        dataset.start()
        i=0
        iterator = iter(dataload)
        for _ in tqdm(range(len(datafiles))):

            # Get the data
            batch = next(iterator)               
            image = batch["image"]
            val_inputs = image.cuda()
            #print(val_inputs.shape)

            # Check to use the correct model for feature extraction
            scaner_label = batch['info']['SeriesDescription'][0].split('_')[0]
            scanner_index = scanner_labels.index(scaner_label)
            if current_fname is None or current_fname != fnames[scanner_index]:
                model = get_model(model_path=fnames[scanner_index])
                model.to(device)
                current_fname = fnames[scanner_index]
            
            val_outputs = model.swinViT(val_inputs)
            latentrep = val_outputs[4] #48*2^4 = 768
            #latentrep = model(val_inputs.to(device))
            #latentrep = model.encoder10(latentrep)
            #print(latentrep.shape)
            record = {
                "SeriesNumber": batch["info"][SERIES_NUMBER_FIELD][0],
                "deepfeatures": latentrep.flatten().tolist(),
                "ROI": batch["roi_label"][0],
                "SeriesDescription": batch["info"][SERIES_DESCRIPTION_FIELD][0],
                "ManufacturerModelName" : batch["info"][MANUFACTURER_MODEL_NAME_FIELD][0],
                "Manufacturer" : batch["info"][MANUFACTURER_FIELD][0],
                "SliceThickness": batch["info"][SLICE_THICKNESS_FIELD][0],        
            }
            writer.writerow(record)
            """#save 3d image
            print("Saving 3d image")
            image = image[0].cpu().numpy()
            image = np.squeeze(image)
            print("Image shape",image.shape)
            image = nib.Nifti1Image(image, np.eye(4))
            name = datafiles[i]["roi"]
            #remobing file path information and only keeping file name of the path
            name = os.path.basename(name)
            nib.save(image, "uncompress_cropped/"+name)
            """
            
        dataset.shutdown()
        
    print("Done !")



def main():
    # fnames = ["./checkpoints/model_swinvit"]
    # fnames = ["./checkpoints/liverrandom_contrast_5_15_10batch_swin"]
    fnames = sorted(glob('./checkpoints/*loso*pth'))
    fnames = [item for item in fnames if not "reconstruction" in item]
    #for fname in fnames:
    #model = get_model(model_path=f"{fname}.pt")
    #model = get_model(model_path=f"{fname}.pth")
    #model = get_model_oscar(path=f"{fname}.pth")
    #device_id = 0
    #os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
    #torch.cuda.set_device(device_id)
    #device = torch.device("cuda" if torch.cuda.is_available() else "cpu") 
    #model = model.to(device)
    run_inference(fnames = fnames, jsonpath = "./train_configurations/expanded_registered_light_dataset_info.json")

if __name__ == "__main__":
    main()