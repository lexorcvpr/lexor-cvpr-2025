from transformers import AutoTokenizer, AutoConfig
import torch
import os
import numpy as np
from segvol.model_segvol_single import build_binary_cube_dict, SegVolModel
from tqdm import tqdm
import json
from glob import glob

def load_item(npz_file, preprocessor):
    file_path = npz_file
    npz = np.load(file_path, allow_pickle=True)
    imgs = npz['imgs']
    print(f"Keys:", list(npz.keys()))
    # gts = npz['gts']
    
    imgs = preprocessor.preprocess_ct_case(imgs)   # (1, H, W, D)
    # Use default box covering the whole image if 'boxes' is missing
    if 'boxes' in npz:
        boxes = npz['boxes']  # a list of bounding box prompts
    else:
        print(f"Warning: 'boxes' missing in {file_path}, using entire image as default")
        _, H, W, D = imgs.shape
        boxes = [[0, 0, 0, H, W, D]]
    
    cube_boxes = []
    el = 0
    for std_box in boxes:
        # Ensure std_box is a dictionary with the required keys
        if isinstance(std_box, list) and len(std_box) == 6:
            std_box = {
                'z_min': std_box[0],
                'z_mid_y_min': std_box[1],
                'z_mid_x_min': std_box[2],
                'z_max': std_box[3],
                'z_mid_y_max': std_box[4],
                'z_mid_x_max': std_box[5]
            }
        el+=1
        binary_cube = build_binary_cube_dict(std_box, imgs.shape[1:])
        cube_boxes.append(binary_cube)
    cube_boxes = torch.stack(cube_boxes, dim=0)
    assert cube_boxes.shape[1:] == imgs.shape[1:], f'{cube_boxes.shape} != {imgs.shape}'

    zoom_item = preprocessor.zoom_transform_case(imgs, cube_boxes)
    zoom_item['file_path'] = file_path
    zoom_item['img_original'] = torch.from_numpy(npz['imgs'])
    return zoom_item

def backfill_foreground_preds(ct_shape, logits_mask, start_coord, end_coord):
    binary_preds = torch.zeros(ct_shape)
    binary_preds[start_coord[0]:end_coord[0], 
                    start_coord[1]:end_coord[1], 
                    start_coord[2]:end_coord[2]] = torch.sigmoid(logits_mask)
    binary_preds = torch.where(binary_preds > 0.5, 1., 0.)
    return binary_preds

def infer_case(model_val, data_item, processor, device):
    data_item['image'], data_item['zoom_out_image'] = \
    data_item['image'].unsqueeze(0).to(device), data_item['zoom_out_image'].unsqueeze(0).to(device)
    start_coord, end_coord = data_item['foreground_start_coord'], data_item['foreground_end_coord']

    img_original = data_item['img_original']
    category_n = data_item['cube_boxes'].shape[0]
    category_ids = torch.arange(category_n) + 1
    category_ids = list(category_ids)
    final_preds = torch.zeros_like(img_original)

    for category_id in category_ids:
        cls_idx = (category_id - 1).item()
        cube_boxes = data_item['cube_boxes'][cls_idx].unsqueeze(0).unsqueeze(0)
        bbox_prompt = processor.bbox_prompt_b(data_item['zoom_out_cube_boxes'][cls_idx], device=device) 
        with torch.no_grad():
            logits_mask = model_val.forward_test(
                image=data_item['image'],
                zoomed_image=data_item['zoom_out_image'],
                bbox_prompt_group=[bbox_prompt, cube_boxes],
                use_zoom=True
            )
        binary_preds = backfill_foreground_preds(img_original.shape, logits_mask, start_coord, end_coord)
        final_preds[binary_preds == 1] = category_id
    
        # clear cache
        torch.cuda.empty_cache()
    return final_preds.numpy()

if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_dir = './segvol'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    # ckpt_path = '/workspace/segvol/weights/best_model.pth'
    ckpt_path = '/workspace/segvol/weights/epoch_50_loss_0.2837_mobilenet_2_5d.pth'
    
    clip_tokenizer = AutoTokenizer.from_pretrained(model_dir)
    config = AutoConfig.from_pretrained(model_dir, trust_remote_code=True)
    model_val = SegVolModel(config)
    model_val.model.text_encoder.tokenizer = clip_tokenizer
    processor = model_val.processor
    checkpoint = torch.load(ckpt_path, map_location=device)
    model_val.load_state_dict(checkpoint['model_state_dict'])
    model_val.eval()
    model_val.to(device)

    # out_dir = "./outputs"
    out_dir = "/workspace/outputs"
    os.makedirs(out_dir, exist_ok=True)

    # npz_files = glob("./3D_val_npz/3D_val_npz/*.npz")
    npz_files = glob("inputs/*.npz")
    missing_boxes_count = 0  # Counter for files missing 'boxes'

    with tqdm(total=len(npz_files), desc="Processing files") as pbar:
        for npz_file in npz_files:
            data_item = load_item(npz_file, processor)
            if 'Warning: \'boxes\' missing' in data_item.get('warnings', ''):
                missing_boxes_count += 1
            final_preds = infer_case(model_val, data_item, processor, device)
            output_path = os.path.join(out_dir, os.path.basename(npz_file))
            np.savez_compressed(output_path, segs=final_preds)
            pbar.update(1)

    print(f'Done. Total files processed: {len(npz_files)}')
    print(f'Total files missing "boxes": {missing_boxes_count}')
