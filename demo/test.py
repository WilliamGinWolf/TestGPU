import gc

import torch
import torch.nn as nn
from torch.nn import init
import torch.nn.functional as F
import scipy.io as sio
import numpy as np
import os
from torch.utils.data import Dataset, DataLoader
import platform
from argparse import ArgumentParser
import random
import csdata_fast2 as csdata_fast
import cv2
import glob
import math
from skimage.metrics import structural_similarity as ssim
# from skimage import measure

from DGUNet_deblock import DGUNet
import utils


parser = ArgumentParser(description='ISTA-Net-plus')

parser.add_argument('--start_epoch', type=int, default=0, help='epoch number of start training')
parser.add_argument('--end_epoch', type=int, default=400, help='epoch number of end training')
parser.add_argument('--layer_num_ICNN', type=int, default=15, help='phase number of ISTA-Net-plus')
parser.add_argument('--layer_num_IFC', type=int, default=5, help='phase number of ISTA-Net-plus')
parser.add_argument('--learning_rate', type=float, default=1e-4, help='learning rate')
parser.add_argument('--group_num', type=int, default=1, help='group number for training')
parser.add_argument('--batch_size', type=int, default=32, help='from {3-10}')

parser.add_argument('--cs_ratio', type=int, default=25, help='from {10, 25, 30, 40, 50}') # default=50

parser.add_argument('--gpu_list', type=str, default='0', help='gpu index')
parser.add_argument('--patch_size', type=int, default=32, help='from {1, 4, 10, 25, 40, 50}')
parser.add_argument('--im_size', type=int, default=128, help='from {1, 4, 10, 25, 40, 50}') # 32 or 128
parser.add_argument('--rgb_range', type=int, default=1, help='value range 1 or 255')
parser.add_argument('--n_channels', type=int, default=1, help='1 for gray, 3 for color')
parser.add_argument('--rb_type', type=int, default=1, help='from {1, 2}')
parser.add_argument('--rb_num', type=int, default=2, help='from {3-10}')

parser.add_argument('--matrix_dir', type=str, default='sampling_matrix_new', help='sampling matrix directory')
parser.add_argument('--model_dir', type=str, default='model', help='trained or pre-trained model directory')
parser.add_argument('--data_dir', type=str, default='Datasets/train/myTrain', help='training data directory')
parser.add_argument('--ext', type=str, default='.jpg', help='training data directory')
parser.add_argument('--log_dir', type=str, default='log', help='log directory')
parser.add_argument('--algo_name', type=str, default='DGUNet_P32_s7_v7_hi3_u4_rp_lm2_learn_BSD_DIV2K_fine', help='log directory')
parser.add_argument('--test_name', type=str, default='myTest', help='name of test set')
parser.add_argument('--num_gpu',type=int,default=1,help='Number of GPU')
parser.add_argument('--loss_mod',type=int,default=1,help='loss mode')

args = parser.parse_args()

start_epoch = args.start_epoch
end_epoch = args.end_epoch
learning_rate = args.learning_rate
layer_num_ICNN = args.layer_num_ICNN
layer_num_IFC = args.layer_num_IFC
group_num = args.group_num
cs_ratio = args.cs_ratio
gpu_list = args.gpu_list
rb_type = args.rb_type
rb_num = args.rb_num
batch_size = args.batch_size

# os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
# os.environ["CUDA_VISIBLE_DEVICES"] = gpu_list
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

ratio_dict = {10: 0, 25: 1, 30: 2, 40: 3, 50: 4}

# n_input = ratio_dict[cs_ratio]
n_output = 1089
nrtrain = 88912  # number of training blocks
psnr_best = 0
best_epoch = 0
# test data
test_name = args.test_name
test_dir = os.path.join('Datasets', test_name)
# filepaths = glob.glob(test_dir + '/*.tif')
filepaths = glob.glob(test_dir + '/*.jpg')
ImgNum = len(filepaths)

# Load CS Sampling Matrix: phi
Phi_data_Name = os.path.join(args.matrix_dir,
                             'phi_sampling_%d_%dx%d.npy' % (args.cs_ratio, args.patch_size, args.patch_size))
Phi_input = np.load(Phi_data_Name)
Phi = torch.from_numpy(Phi_input).type(torch.FloatTensor)
# Phi = Phi.to(device)  # .unsqueeze(0).unsqueeze(0)


length, in_channels = Phi.shape
print('Length:', length, ' In_channels:', in_channels)
model = DGUNet(in_c=1, out_c=1,cs_ratio=args.cs_ratio)#ISTANetplus(layer_num_ICNN, layer_num_IFC, length=length, in_channels=in_channels)
print('GPU: ',list(range(args.num_gpu)))
model = nn.DataParallel(model,device_ids=list(range(args.num_gpu)))
# model = nn.DataParallel(model,device_ids=[0,1])
model = model.to(device)
model_dir = "./%s/CS_%s_layerICNN_%d_layerIFC_%s_group_%d_ratio_%d" % (
args.model_dir, args.algo_name, layer_num_ICNN, layer_num_IFC, group_num, cs_ratio)
result_dir="./results/%s_CS_%s_layerICNN_%d_layerIFC_%s_group_%d_ratio_%d" % (args.test_name,args.algo_name, layer_num_ICNN, layer_num_IFC, group_num, cs_ratio)
if not os.path.exists(result_dir):
    os.makedirs(result_dir)
# Load pre-trained model with epoch number
model.load_state_dict(torch.load('%s/net_best.pkl' % (model_dir)))

def col2im_CS_py(X_col, row, col, row_new, col_new):
    block_size = args.im_size
    X0_rec = np.zeros([row_new, col_new])
    count = 0
    for x in range(0, row_new - block_size + 1, block_size):
        for y in range(0, col_new - block_size + 1, block_size):
            X0_rec[x:x + block_size, y:y + block_size] = X_col[:, count].reshape([block_size, block_size])
            count = count + 1
    X_rec = X0_rec[:row, :col]
    return X_rec


def psnr(img1, img2):
    img1.astype(np.float32)
    img2.astype(np.float32)
    mse = np.mean((img1 - img2) ** 2)
    if mse == 0:
        return 100
    PIXEL_MAX = 255.0
    return 20 * math.log10(PIXEL_MAX / math.sqrt(mse))


def imread_CS_py(Iorg):
    block_size = args.im_size
    [row, col] = Iorg.shape
    row_pad = block_size - np.mod(row, block_size)
    col_pad = block_size - np.mod(col, block_size)
    Ipad = np.concatenate((Iorg, np.zeros([row, col_pad])), axis=1)
    Ipad = np.concatenate((Ipad, np.zeros([row_pad, col + col_pad])), axis=0)
    [row_new, col_new] = Ipad.shape

    return [Iorg, row, col, Ipad, row_new, col_new]


def img2col_py(Ipad, block_size):
    [row, col] = Ipad.shape
    row_block = row / block_size
    col_block = col / block_size
    block_num = int(row_block * col_block)
    img_col = np.zeros([block_size ** 2, block_num])
    count = 0
    for x in range(0, row - block_size + 1, block_size):
        for y in range(0, col - block_size + 1, block_size):
            img_col[:, count] = Ipad[x:x + block_size, y:y + block_size].reshape([-1])
            count = count + 1
    return img_col


print_flag = 1  # print parameter number

if print_flag:
    num_count = 0
    num_params = 0
    for para in model.parameters():
        num_count += 1
        num_params += para.numel()
#         print('Layer %d' % num_count)
#         print(para.size())
    print("total para num: %d" % num_params)


class RandomDataset(Dataset):
    def __init__(self, data, length):
        self.data = data
        self.len = length

    def __getitem__(self, index):
        return torch.Tensor(self.data[index, :]).float()

    def __len__(self):
        return self.len

def augment_img_tensor(img, mode=0):
    img_size = img.size()
    img_np = img.data.cpu().numpy()
    if len(img_size) == 3:
        img_np = np.transpose(img_np, (1, 2, 0))
    elif len(img_size) == 4:
        img_np = np.transpose(img_np, (2, 3, 1, 0))
    img_np = augment_img(img_np, mode=mode)
    img_tensor = torch.from_numpy(np.ascontiguousarray(img_np))
    if len(img_size) == 3:
        img_tensor = img_tensor.permute(2, 0, 1)
    elif len(img_size) == 4:
        img_tensor = img_tensor.permute(3, 2, 0, 1)

    return img_tensor.type_as(img)


def augment_img(img, mode=0):
    if mode == 0:
        return img
    elif mode == 1:
        return np.flipud(np.rot90(img))
    elif mode == 2:
        return np.flipud(img)
    elif mode == 3:
        return np.rot90(img, k=3)
    elif mode == 4:
        return np.flipud(np.rot90(img, k=2))
    elif mode == 5:
        return np.rot90(img)
    elif mode == 6:
        return np.rot90(img, k=2)
    elif mode == 7:
        return np.flipud(np.rot90(img, k=3))

def test_x8(model, L):
    E_list = [model(augment_img_tensor(L, mode=i))[0] for i in range(8)]
    for i in range(len(E_list)):
        if i == 3 or i == 5:
            E_list[i] = augment_img_tensor(E_list[i], mode=8 - i)
        else:
            E_list[i] = augment_img_tensor(E_list[i], mode=i)
    output_cat = torch.stack(E_list, dim=0)
    E = output_cat.mean(dim=0, keepdim=False)
    return E

# Testing
model.eval()
with torch.no_grad():
    psnr_ave = 0
    ssim_ave = 0
    for img_no in range(ImgNum):
        imgName = filepaths[img_no]
        Img = cv2.imread(imgName, 1)
        Img_yuv = cv2.cvtColor(Img, cv2.COLOR_BGR2YCrCb)
        Iorg_y = Img_yuv[:, :, 0]
        [Iorg, row, col, Ipad, row_new, col_new] = imread_CS_py(Iorg_y)
        Icol = img2col_py(Ipad, args.im_size).transpose() / 255.0
        Img_output = Icol
        batch_x = torch.from_numpy(Img_output)
        batch_x = batch_x.type(torch.FloatTensor)
        batch_x = batch_x.to(device)
        batch_x = batch_x.view(batch_x.shape[0], 1, args.im_size, args.im_size)
        x_output = model(batch_x)#test_x8(model,batch_x)#model(batch_x)#[0]  # torch.mm(batch_x,

        batch_x.cpu()
        del batch_x
        gc.collect()

        x_output=x_output[0]

        x_output = x_output.view(x_output.shape[0], -1)
        Prediction_value = x_output.cpu().data.numpy()
        X_rec = np.clip(col2im_CS_py(Prediction_value.transpose(), row, col, row_new, col_new), 0, 1)
        utils.save_img((os.path.join(result_dir, imgName.split('/')[-1])), (X_rec*255.).astype(np.uint8))

        rec_PSNR = psnr(X_rec * 255, Iorg.astype(np.float64))
        rec_ssim = ssim(X_rec * 255, Iorg.astype(np.float64), data_range=255)
        print(imgName.split('/')[-1],rec_PSNR)
        psnr_ave += rec_PSNR
        ssim_ave += rec_ssim
        del x_output
psnr_ave /= ImgNum
ssim_ave /= ImgNum
print('best ssim is %.4f best psnr is %.4f in epoch %d psnr_rec: %.4f' % (ssim_ave,psnr_best, best_epoch, psnr_ave))