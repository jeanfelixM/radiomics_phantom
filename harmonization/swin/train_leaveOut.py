import sys
sys.path.append('/home/reza/radiomics_phantom/')
import time
import glob
import json
import os
import re
from matplotlib import pyplot as plt
import numpy as np
from sklearn.model_selection import GroupShuffleSplit
from tqdm import tqdm
from analyze.analyze import perform_tsne
import torch
from torch.optim.lr_scheduler import CosineAnnealingLR
import tensorflow as tf
import torch.optim as optim
from analyze.classification import save_results_to_csv,define_classifier
from pytorch_msssim import ssim
from monai.data import DataLoader, Dataset,CacheDataset,PersistentDataset,SmartCacheDataset,ThreadDataLoader
from monai.transforms import Compose, LoadImaged, EnsureChannelFirstd, AsDiscreted, ToTensord,EnsureTyped,RandCropd,RandSpatialCropd
from harmonization.swin.extract import CropOnROId, custom_collate_fn,DebugTransform
from harmonization.swin.utils import plot_multiple_losses, load_data,save_losses,get_model, load_subbox_positions, get_oscar_for_training
from monai.networks.nets import SwinUNETR
from pytorch_metric_learning.losses import NTXentLoss
from monai.transforms import Transform
import logging
import numpy as np
from monai.transforms import Transform
from monai.data import ITKReader
import itk
from random import shuffle
from keras.utils import to_categorical
import torch.nn as nn
import threading
from scipy.spatial import procrustes
import imageio
import nibabel as nib
import os
import argparse
os.environ["CUDA_VISIBLE_DEVICES"] = "0"  

import torch
torch.cuda.set_device(0)


gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
    except RuntimeError as e:
        print(e)
import tensorflow as tf
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        # N'utilise que le GPU 0
        tf.config.experimental.set_visible_devices(gpus[0], 'GPU')
        tf.config.experimental.set_memory_growth(gpus[0], True)
    except RuntimeError as e:
        print(e)

def align_embeddings(reference, embeddings):
    _, aligned_embeddings, _ = procrustes(reference, embeddings)
    return aligned_embeddings

class ReconstructionLoss(nn.Module):
    def __init__(self, ssim_weight=0.5):
        super(ReconstructionLoss, self).__init__()
        self.l1_loss = nn.L1Loss()
        self.ssim_weight = ssim_weight

    def forward(self, output, target):
        l1 = self.l1_loss(output, target)
        ssim_loss = 1 - ssim(output, target, data_range=1.0, size_average=True)
        #print(f"l1: {l1}, ssim: {ssim_loss}")
        total_loss = l1 + self.ssim_weight * ssim_loss
        return total_loss

def get_device():
        device_id = int(0)
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
        torch.cuda.set_device(device_id)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return device
    
class OrthogonalityLoss:
    def __init__(self, batch_size, num_feature_maps, feature_shape, device, split=None):

        self.batch_size = batch_size
        self.num_feature_maps = num_feature_maps
        self.feature_shape = feature_shape
        self.device = device
        self.split = split

        self.create_identity_and_mask()

    def create_identity_and_mask(self):
        # Create the identity matrix
        identity_matrix = torch.eye(self.num_feature_maps, device=self.device)  # [num_feature_maps x num_feature_maps]
        identity_matrix = identity_matrix.unsqueeze(0)  # [1 x num_feature_maps x num_feature_maps]
        identity_matrix = identity_matrix.expand(self.batch_size, -1,  -1)  # [batch_size x num_feature_maps x num_feature_maps]

        # Create a mask to zero out diagonal elements and elements not involved in the calculation
        mask = torch.ones(self.num_feature_maps, self.num_feature_maps, device=self.device)
        if self.split is not None:
            mask[:, :self.split] = 0
            mask[self.split:, :] = 0
            mask[:self.split, self.split:] = 1
            mask[self.split:, :self.split] = 1
        mask[range(self.num_feature_maps), range(self.num_feature_maps)] = 0
        mask = mask.unsqueeze(0).expand(self.batch_size, -1, -1)  # [batch_size x num_feature_maps x num_feature_maps]

        self.identity_matrix = identity_matrix
        self.mask = mask

    def __call__(self, H):

        print("H size",H.size())
        if H.shape[0] != self.batch_size:
            self.batch_size = H.shape[0]
            self.create_identity_and_mask()
        

        # Reshape H to a 2D tensor for matrix multiplication
        H_reshaped = H.view(self.batch_size,  self.num_feature_maps, -1)  # [batch_size x num_feature_maps x product of feature_shape]

        # Compute H H^T
        H_Ht = torch.matmul(H_reshaped, H_reshaped.transpose(-1, -2))  # [batch_size x num_feature_maps x num_feature_maps]

        # Apply the mask to the difference (H H^T - I)
        masked_diff = (H_Ht - self.identity_matrix) * self.mask

        # Compute the orthogonality loss: || masked_diff ||
        L = torch.norm(masked_diff)

        return L

class Train:
    def __init__(self, model, data_loader, optimizer, lr_scheduler, num_epoch, dataset, classifier=None, acc_metric='total_mean', contrast_loss=NTXentLoss(temperature=0.20), contrastive_latentsize=768,savename='model.pth',ortho_reg=0.05,to_compare=False):
        self.model = model
        self.model = self.model.double()
        self.in_channels = 1
        self.classifier = classifier
        self.data_loader = data_loader
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.num_epoch = num_epoch
        self.contrast_loss = contrast_loss
        self.classification_loss = torch.nn.CrossEntropyLoss().cuda()
        self.device = get_device()
        self.recons_loss = ReconstructionLoss(ssim_weight=5).to(self.device)
        self.acc_metric = acc_metric
        self.ortho_reg = ortho_reg
        self.batch_size = data_loader['train'].batch_size
        self.dataset = dataset['train']
        self.testdataset = dataset['test']
        self.contrastive_latentsize = contrastive_latentsize
        self.save_name = savename
        self.reconstruct = self.get_reconstruction_model()
        self.reference_embeddings_2d = None
        self.to_compare = to_compare
        #quick fix to load reconstruction model
        #self.load_reconstruction_model('FT_whole_RECONSTRUCTION_model_reconstruction.pth')
        

        # Orthogonality loss
        self.orth_loss = OrthogonalityLoss(batch_size=self.batch_size, num_feature_maps=768, feature_shape=(2, 2, 1), device=self.device, split=self.contrastive_latentsize)
        
        #quick fix to train decoder only
        #self.optimizer = optim.Adam(self.reconstruct.parameters(), lr=1e-3) #ajouter tout
        
        
        
        self.epoch = 0
        self.log_summary_interval = 5
        self.step_interval = 10
        self.total_progress_bar = tqdm(total=self.num_epoch, desc='Total Progress', dynamic_ncols=True)
        self.acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.losses_dict = {'total_loss': 0, 'src_classification_loss': 0, 'contrast_loss': 0, 'reconstruction_loss': 0, 'orthogonality_loss': 0}
        self.log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        self.best_acc_dict = {'src_best_train_acc': 0, 'src_best_test_acc': 0, 'tgt_best_test_acc': 0}
        self.best_loss_dict = {'total_loss': float('inf'), 'src_classification_loss': float('inf'), 'contrast_loss': float('inf')}
        self.best_log_dict = {'src_train_acc': 0, 'src_test_acc': 0, 'tgt_test_acc': 0}
        self.tsne_plots = []
        
        if self.to_compare:
            from monai.losses import DiceCELoss
            # non_swinvit_params = [p for name, p in model.named_parameters() if not name.startswith('swinViT')]
            # print(f"Number of parameters in the model: {sum(p.numel() for p in model.parameters())}")
            # print(f"Number of parameters in the non-SwinViT part of the model: {sum(p.numel() for p in non_swinvit_params)}")
            self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=1e-4, weight_decay=1e-5)
            self.diceloss = DiceCELoss(to_onehot_y=True, softmax=True)
            self.losses_dict['dice_loss'] = 0.0
            self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=40, gamma=0.1)
        
        self.train_losses = {'contrast_losses': [], 'classification_losses': [], 'reconstruction_losses': [], 'orthogonality_losses': [], 'total_losses': [], 'dice_losses': []}
    
        
    def get_reconstruction_model(self, reconstruction_type='vae',dim=768):
        if reconstruction_type == 'vae':
            model = nn.Sequential(
                nn.Conv3d(dim, dim // 2, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 2),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 2, dim // 4, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 4),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 4, dim // 8, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 8),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 8, dim // 16, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 16),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 16, dim // 16, kernel_size=3, stride=1, padding=1),
                nn.InstanceNorm3d(dim // 16),
                nn.LeakyReLU(),
                nn.Upsample(scale_factor=2, mode="trilinear", align_corners=False),
                nn.Conv3d(dim // 16, self.in_channels, kernel_size=1, stride=1),
            )
            model.to(self.device)
            return model
        elif reconstruction_type == 'deconv':
            model= nn.Sequential(
                nn.ConvTranspose3d(dim, dim // 2, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 2, dim // 4, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 4, dim // 8, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 8, dim // 16, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
                nn.ConvTranspose3d(dim // 16, self.in_channels, kernel_size=(2, 2, 2), stride=(2, 2, 2)),
            )
            model.to(self.device)
            return model
        else:
            raise ValueError(f"Invalid reconstruction type: {reconstruction_type}")
        
    def load_reconstruction_model(self, path):
        weights = torch.load(path)
        self.reconstruct.load_state_dict(weights)
        self.reconstruct.eval()
        print(f'Model weights loaded from {path}')
            
    def plot_losses(self):
        plot_multiple_losses(self.train_losses, self.step_interval)

    
    def train(self):
        self.total_progress_bar.write('Start training')
        self.dataset.start()
        self.testdataset.start()
        self.model.train()
        while self.epoch < self.num_epoch:
            self.train_loader = self.data_loader['train'] #il faudra que le dataloader monai ne mette pas dans le meme batch des ct scan de la meme serie (cad des memes repetitions d'un scan) -> voir Sampler pytorch
            self.test_loader = self.data_loader['test']
            self.train_epoch()
            if self.epoch % self.log_summary_interval == 0:
                #self.test_epoch()
                #self.testdataset.update_cache()
                #self.log_summary_writer()
                pass
            self.lr_scheduler.step()
            self.dataset.update_cache()
            if self.epoch % 3 == 1 or self.epoch == self.num_epoch :
                try:
                    self.latents_t,self.labels_t,self.latents_v,self.labels_v,self.groups = self.plot_latent_space(self.epoch)
                except Exception as e:
                    print(f"Error plotting latent space: {e}")
        
        self.dataset.shutdown()
        self.testdataset.shutdown()
        self.total_progress_bar.write('Finish training')
        self.save_model(self.save_name)
        reconstruction_model_path = self.save_name.replace('.pth', '_reconstruction.pth')
        self.save_reconstruction_model(reconstruction_model_path)
        try:
            self.create_gif()
            self.plot_losses()
        except Exception as e:
            print(f"Error plotting losses or creating gif")
        try :
            return self.latents_t,self.labels_t,self.latents_v,self.labels_v,self.groups
        except Exception as e:
            print(f"Error returning latents and labels: {e}")
            return None,None,None,None,None

    def train_epoch(self):
        epoch_iterator = tqdm(self.train_loader, desc="Training (X / X Steps) (loss=X.X)", dynamic_ncols=True)
        total_batches = len(self.train_loader)
        running_loss = 0
        
        for step, batch in enumerate(epoch_iterator):
            
            loss,classif_acc = self.train_step(batch)
            running_loss += loss['total_loss'].item()
            average_loss = running_loss / (step + 1)
            epoch_iterator.set_description("Training ({}/ {}) (loss={:.4f}), epoch contrastive loss={:.4f}, epoch classification loss={:.4f}, classif_acc={:.4f}".format(step + 1, total_batches, average_loss,loss['contrast_loss'],loss['classification_loss'],classif_acc))
            # todo orthogonality

            epoch_iterator.refresh()
            if step % self.step_interval == 0:
                self.save_losses(average_loss,loss_file=f'{self.save_name}')
        self.total_progress_bar.update(1)
        self.epoch += 1
        
        
    def train_step(self,batch):
        # update the learning rate of the optimizer
        self.optimizer.zero_grad()
        # prepare batch
        imgs_s = batch["image"].cuda()
        imgs_s = imgs_s.squeeze()
        #ids = batch["uids"].cuda()
        print("imgs_s size 1",imgs_s.size())
        #print("ids size",ids.size())
        imgs_s = imgs_s.view(imgs_s.shape[0] * imgs_s.shape[1],1, imgs_s.shape[2], imgs_s.shape[3], imgs_s.shape[4]) 
        #ids = ids.view(imgs_s.shape[0] * imgs_s.shape[1])
        print("imgs_s size 2",imgs_s.size())
        #print("ids size",ids.size())
    
        # encoder inference
        #all_labels = batch["roi_label"].cuda()
        #ids = all_labels
        ids = batch["uids"].cuda()
        print("ids size 1",ids.size())
        ids = ids.view(imgs_s.shape[0] * imgs_s.shape[1])
        print("ids size 2",ids.size())
        scanner_labels = batch["scanner_label"].cuda()
        
        latents = self.model.swinViT(imgs_s.double())
        
        
        #narrow the latents to use the contrastive latent space (maybe pass to encoder10 for latents[4] before contrastive loss ?)
        nlatents4, bottleneck = torch.split(latents[4], [self.contrastive_latentsize, latents[4].size(1) - self.contrastive_latentsize], dim=1)
        nlatents = [latents[0], latents[1], latents[2], latents[3],0]
        nlatents[4] = nlatents4
        #print("bottleneck size",bottleneck.size())
        #print("nlatents[4] size",nlatents[4].size())
        
        #print("ids size",ids.size())
        self.contrastive_step(nlatents,ids.flatten(),latentsize = self.contrastive_latentsize)
        #print(f"Contrastive Loss: {self.losses_dict['contrast_loss']}")
        
        # features = torch.mean(bottleneck, dim=(2, 3, 4))
        # accu = self.classification_step(features, scanner_labels)
        #print(f"Train Accuracy: {accu}%")
        accu = 0
        self.losses_dict['classification_loss'] = 0.0
        # Orthogonality loss
        #self.losses_dict['orthogonality_loss'] =  self.orth_loss(latents[4])
        print(f"Orthogonality Loss: {self.losses_dict['orthogonality_loss']}")
        #image reconstruction (either segmentation using the decoder or straight reconstruction using a deconvolution)
        # reconstructed_imgs = self.reconstruct_image(latents[4]) 
        
        # #saving nifti image to disk
        # img = reconstructed_imgs[0,:,:,:,:].detach().cpu().numpy()
        # img = np.squeeze(img)
        # img = nib.Nifti1Image(img, np.eye(4))
        # nib.save(img, "reconstructed_image.nii")
        
        #saving original image to disk
        # img = imgs_s[0,:,:,:,:].detach().cpu().numpy()
        # img = np.squeeze(img)
        # img = nib.Nifti1Image(img, np.eye(4))
        # nib.save(img, "original_image.nii")
        
        
        #self.reconstruction_step(reconstructed_imgs, imgs_s) 
        self.losses_dict['reconstruction_loss'] = 0.0
        # if self.epoch >= 5:
        #     self.losses_dict['total_loss'] = \
        #     self.ortho_reg*self.losses_dict['orthogonality_loss'] + self.losses_dict['contrast_loss'] + self.losses_dict['reconstruction_loss'] +  self.losses_dict['classification_loss'] 
        # 
        # elif self.epoch >= 2:
        #     self.losses_dict['total_loss'] = \
        #     self.losses_dict['contrast_loss'] + self.losses_dict['reconstruction_loss'] + self.losses_dict['classification_loss'] 
        # else:
        #     self.losses_dict['total_loss'] = self.losses_dict['contrast_loss']
        self.losses_dict['total_loss'] = \
        self.losses_dict['contrast_loss']
            
            
        self.losses_dict['total_loss'].backward()
        self.optimizer.step()
        return self.losses_dict, accu

    def classification_step(self, features, labels):
        #print(f"the labels is {labels}")
        if self.classifier is None:
            self.classifier = self.autoclassifier(features.size(1), 13)
        logits = self.classifier(features)
        #print(f"the logits is {logits}")
        classification_loss = self.classification_loss(logits, labels)
        self.losses_dict['classification_loss'] = classification_loss
        
        return compute_accuracy(logits, labels, acc_metric=self.acc_metric)

    def contrastive_step(self, latents,ids,latentsize = 768): #actuellement la loss contrastive est aussi calculé entre sous patchs de la même image, on voudrait eviter ça idealement
        #print("ids",ids)
        
        total_num_elements = latents[4].shape[0] * latents[4].shape[2] * latents[4].shape[3] * latents[4].shape[4]
        all_embeddings = torch.empty(total_num_elements, latentsize)
        all_labels = torch.empty(total_num_elements, dtype=torch.long)
        
        offset = 0
        start_idx = 0
        for id in torch.unique(ids):
            #print("id",id)
            boolids = (ids == id)
            #print("boolids",boolids)
            
            #bottleneck
            #print("latents size",len(latents))
            btneck = latents[4]  # (batch_size, latentsize, D, H, W)
            #print("btneck size",btneck.size())
            btneck = btneck[boolids]
            #print("new btneck size",btneck.size())
            num_elements = btneck.shape[2] * btneck.shape[3] * btneck.shape[4]
            #print("num_elements",num_elements)
        
            # (nbatch_size, 768,D, H, W) -> (nbatch_size * num_elements, latentsize)
            embeddings = btneck.permute(0, 2, 3, 4, 1).reshape(-1, latentsize)
            
            #contrast_ind = torch.arange(offset,offset+num_elements) #with this one under patch of the cropped ROI patch will be compared to each other : negatives within same roi
            contrast_ind = torch.full((num_elements,), offset) #negatives only between different r
            labels = contrast_ind.repeat(btneck.shape[0]) 
            #print("weigth",weigth)
            #print("embeddings size",embeddings.size())
            #print("labels size",labels.size())           
            end_idx = start_idx + embeddings.shape[0]
            all_embeddings[start_idx:end_idx, :] = embeddings
            all_labels[start_idx:end_idx] = labels #it is the tensor that contains the info about positive and negative pairs, as i explained in the mail to rezza
            start_idx = end_idx
            
            offset += num_elements
        
        llss = (self.contrast_loss(all_embeddings, all_labels)) #how is the loss computed ? see here : https://github.com/KevinMusgrave/pytorch-metric-learning/discussions/698#discussion-6811873 and the referenced doc
        self.losses_dict['contrast_loss'] = llss
        
    def reconstruction_step(self, reconstructed_imgs, original_imgs): 
        reconstruction_loss = self.recons_loss(reconstructed_imgs, original_imgs)
        self.losses_dict['reconstruction_loss'] = reconstruction_loss
        
    def reconstruct_image(self,bottleneck): 
        _, c, h, w, d = bottleneck.shape
        x_rec = bottleneck.flatten(start_dim=2, end_dim=4)
        x_rec = x_rec.view(-1, c, h, w, d)
        x_rec = self.reconstruct(x_rec)
        
        return x_rec
        
    def test_epoch(self):
        self.model.eval()
        total_test_accuracy = []
        with torch.no_grad():
            testing_iterator = tqdm(self.test_loader, desc="Testing (X / X Steps) (loss=X.X)", dynamic_ncols=True)
            running_val_loss = 0
            for step,batch in enumerate(testing_iterator):
                imgs_s = batch["image"].cuda()
                all_labels = batch["roi_label"].cuda()
                #logits = self.classifier(torch.mean(self.model.swinViT(imgs_s)[4], dim=(2, 3, 4)))
                #test_accuracy = compute_accuracy(logits, all_labels, acc_metric=self.acc_metric)
                test_accuracy = 0
                total_test_accuracy.append(test_accuracy)
                running_val_loss += self.losses_dict['total_loss'].item()
                testing_iterator.set_description(f"Testing ({step + 1}/{len(self.test_loader)}) (accuracy={test_accuracy:.4f})")
        avg_test_accuracy = np.mean(total_test_accuracy)
        avg_val_loss = running_val_loss / len(self.test_loader)
        self.acc_dict['best_test_acc'] = avg_test_accuracy
        #self.save_losses(self.train_losses[-1] if self.train_losses else None, avg_val_loss)  # Save the validation loss
        print(f"Test Accuracy: {avg_test_accuracy}%")
        
    def autoclassifier(self, in_features, num_classes):
        #simple mlp with dropout
        classifier = torch.nn.Sequential(
            torch.nn.Linear(in_features, 512),
            torch.nn.ReLU(),
            torch.nn.Dropout(0.2),
            torch.nn.Linear(512, num_classes)
        ).cuda()
        return classifier 
    
    def save_model(self, path):
        torch.save(self.model.state_dict(), path)
        print(f'Model weights saved to {path}')
        
    def save_reconstruction_model(self, path):
        torch.save(self.reconstruct.state_dict(), path)
        print(f'Model weights saved to {path}')

    def save_losses(self, train_loss, loss_file='losses.json'):
        self.train_losses['total_losses'].append(train_loss)
        self.train_losses['contrast_losses'].append(self.losses_dict['contrast_loss'])
        self.train_losses['classification_losses'].append(self.losses_dict['classification_loss'])
        self.train_losses['reconstruction_losses'].append(self.losses_dict['reconstruction_loss'])
        self.train_losses['orthogonality_losses'].append(self.losses_dict['orthogonality_loss'])
        
        if self.to_compare:
            self.train_losses['dice_losses'].append(self.losses_dict['dice_loss'])
        
        save_losses(self.train_losses, loss_file,to_compare=self.to_compare)
        

    def plot_latent_space(self, epoch):
        self.model.eval() 
        latents = []
        labels = [] 
        scanner_labels = []
        latents_v = []
        labels_v = [] 

        with torch.no_grad():
            for batch in self.data_loader['train']:
                images = batch['image'].cuda()
                latents_tensor = self.model.swinViT(images)[4]
                
                batch_size, channels, *dims = latents_tensor.size()
                flatten_size = torch.prod(torch.tensor(dims)).item()
                
                latents_tensor = latents_tensor.reshape(batch_size, channels * flatten_size)
                latents.extend(latents_tensor.cpu().numpy())
                labels.extend(batch['roi_label'].cpu().numpy()) 
                scanner_labels.extend(batch['scanner_label'].cpu().numpy())
            
            for batch in self.data_loader['test']:
                images = batch['image'].cuda()
                latents_tensor = self.model.swinViT(images)[4]
                
                batch_size, channels, *dims = latents_tensor.size()
                flatten_size = torch.prod(torch.tensor(dims)).item()
                
                latents_tensor = latents_tensor.reshape(batch_size, channels * flatten_size)
                latents_v.extend(latents_tensor.cpu().numpy())
                labels_v.extend(batch['roi_label'].cpu().numpy())

        latents_2d = perform_tsne(latents)

        if self.reference_embeddings_2d is None:
            self.reference_embeddings_2d = latents_2d
        else:
            latents_2d = align_embeddings(self.reference_embeddings_2d, latents_2d)

        plt.figure(figsize=(10, 10))
        scatter = plt.scatter(latents_2d[:, 0], latents_2d[:, 1], c=labels, cmap='viridis')
        plt.colorbar(scatter, label='Labels')
        plt.title(f'Latent Space t-SNE at Epoch {epoch}')
        plt.xlabel('Dimension 1')
        plt.ylabel('Dimension 2')

        plot_path =  f'{self.save_name}_latent_space_tsne_epoch_{epoch}.png'
        print(f'Saving latent space plot to {plot_path}')
        plt.savefig(plot_path)
        plt.close()  

        self.tsne_plots.append(plot_path)
        return latents,labels,latents_v,labels_v,scanner_labels

    def create_gif(self):
        images = []
        for plot_path in self.tsne_plots:
            images.append(imageio.imread(plot_path))
        imageio.mimsave(self.save_name+'_latent_space_evolution.gif', images, duration=1)
    
def compute_accuracy(logits, true_labels, acc_metric='total_mean', print_result=False): #a revoir
    assert logits.size(0) == true_labels.size(0)
    if acc_metric == 'total_mean':
        predictions = torch.max(logits, dim=1)[1]
        accuracy = 100.0 * (predictions == true_labels).sum().item() / logits.size(0)
        if print_result:
            print(accuracy)
        return accuracy
    elif acc_metric == 'class_mean':
        num_classes = logits.size(1)
        predictions = torch.max(logits, dim=1)[1]
        class_accuracies = []
        for class_label in range(num_classes):
            class_mask = (true_labels == class_label)

            class_count = class_mask.sum().item()
            if class_count == 0:
                class_accuracies += [0.0]
                continue

            class_accuracy = 100.0 * (predictions[class_mask] == class_label).sum().item() / class_count
            class_accuracies += [class_accuracy]
        if print_result:
            print(f'class_accuracies: {class_accuracies}')
            print(f'class_mean_accuracies: {torch.mean(class_accuracies)}')
        return torch.mean(class_accuracies)
    else:
        raise ValueError(f'acc_metric, {acc_metric} is not available.')
    

   
    
def group_data(data_list, mode='scanner'):
    group_map = {}

    # Helper function to extract base description for 'repetition' mode
    def extract_base(description):
        base = re.match(r"(.+)(-\s#\d+)$", description)
        if base:
            return base.group(1).strip()
        return description

    group_ids = []  
    for item in data_list:
        series_description = item['info']['SeriesDescription']
        if mode == 'scanner':
            group_key = series_description[:2]
        elif mode == 'repetition':
            group_key = extract_base(series_description)
        
        if group_key not in group_map:
            group_map[group_key] = len(group_map)
        
        item['group_id'] = group_map[group_key]
        group_ids.append(item['group_id']) 
    print("Groups correspondance", group_map)
    return np.array(group_ids)

def create_datasets(data_list, test_size=0.2, seed=42):
    
    if test_size <= 0.00000001:
        return data_list, []
    
    groups = group_data(data_list, mode='scanner') 
    
    
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_idx, test_idx = next(splitter.split(data_list, groups=groups))

    train_data = [data_list[i] for i in train_idx]
    test_data = [data_list[i] for i in test_idx]
   
    print(f"Number of training samples: {len(train_data)}")
    print(f"Number of testing samples: {len(test_data)}")
    train_groups = np.unique(groups[train_idx])
    val_groups = np.unique(groups[test_idx])
    print(f'Training groups: {train_groups}')
    print(f'Validation groups: {val_groups}')

    return train_data, test_data


class EncodeLabels(Transform):
    def __init__(self, encoder, key='roi_label'):
        self.encoder = encoder
        self.key = key

    def __call__(self, data):
        data[self.key] = self.encoder.transform([data[self.key]])[0]  # Encode the label
        return data

class DebugTransform2(Transform):
    def __call__(self, data):
        print("Image shape:", data['image'].shape)
        #print("Encoded label:", data['roi_label'], "Type:", type(data['roi_label']))
        # Optionally, check the unique values in the label if it's a segmentation map
        #if isinstance(data['roi_label'], np.ndarray):
        #    print("Unique values in label:", np.unique(data['roi_label']))
        return data

class ExtractScannerLabel(Transform):
    def __call__(self, data):
        data['scanner_label'] = data['info']['SeriesDescription'][:2]
        return data

class PrintDebug(Transform):
    def __call__(self, data):
        print("Debugging")
        return data

class LazyPatchLoader(Transform):
    """
    A transformation class for lazy loading and extracting 3D patches from medical images.

    This class extracts a specified number of 3D patches of a given size from medical images.
    It uses precomputed random positions for patch extraction to introduce variability
    during training. The patches are extracted using the ITK library, which allows efficient
    handling of large medical images.

    Attributes:
        roi_size (tuple): The size of the region of interest (ROI) to be extracted, specified
                          as (width, height, depth). Default is (64, 64, 32).
        num_patches (int): The number of patches to extract from each image. Default is 5.
        variety_size (int): The number of random positions to precompute for patch extraction,
                            providing variability. Default is 15.
        reader (object): The image reader object used to load images. If not provided, an
                         ITKReader is used by default.
        positions_file (str): Path to a JSON file containing precomputed valid positions for
                              patch extraction. Default is "output/valid_positions2_positions.json".
        precomputed_positions (list): A list of precomputed positions used for extracting patches.
        current_position_index (int): The index of the current position being used for extraction.
        logger (object): Logger instance for logging information and errors.

    Methods:
        precompute_positions(shape):
            Precomputes and shuffles random positions for patch extraction based on the image shape.

        __call__(data):
            Main method for loading the image, extracting patches, and returning the modified data dictionary.

    Example usage:
        transforms = Compose([
            LoadImaged(keys=["image"]),
            LazyPatchLoader(roi_size=[64, 64, 32]),
            EnsureTyped(keys=["image"], device=device, track_meta=False),
        ])
    """

    def __init__(self, roi_size=(64, 64, 32), num_patches=5, variety_size=15, reader=None, positions_file="output/valid_positions2_positions.json"):
        self.position_file = positions_file
        self.roi_size = roi_size
        self.num_patches = num_patches  # Number of patches to extract
        self.reader = reader or ITKReader()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.variety_size = variety_size
        self.precomputed_positions = None#load_subbox_positions(positions_file, order='XYZ', num_positions=variety_size)
        self.current_position_index = 0

    def precompute_positions(self, shape):
        """
        Precompute and shuffle random positions for patch extraction.

        Args:
            shape (tuple): The shape of the image from which patches are to be extracted.

        This method generates a list of random positions within the image based on the
        specified ROI size and the provided image shape. The positions are then shuffled
        to introduce randomness during patch extraction.
        """
        self.precomputed_positions = []
        for _ in range(self.variety_size):
            start_x = np.random.randint(0, shape[0] - self.roi_size[0] + 1)
            start_y = np.random.randint(0, shape[1] - self.roi_size[1] + 1)
            start_z = np.random.randint(0, shape[2] - self.roi_size[2] + 1)
            self.precomputed_positions.append((start_x, start_y, start_z))
        shuffle(self.precomputed_positions)
        print("Precomputed positions",self.precomputed_positions)

    def __call__(self, data):
        """
        Load the image, extract patches, and return the modified data dictionary.

        Args:
            data (dict): A dictionary containing image data. Expects a key 'image' with the image path.

        Returns:
            dict: A modified dictionary containing extracted patches and their unique identifiers.

        This method performs the following steps:
        1. Loads the image using the specified reader.
        2. Validates the image size against the ROI size.
        3. Extracts the specified number of patches from the image at precomputed positions.
        4. Returns the patches and unique identifiers in the data dictionary.
        """
        try:
            image_path = data['image']
            self.logger.info(f"Loading image from path: {image_path}")
            
            img_obj = self.reader.read(image_path)
            self.logger.info(f"Image object loaded: {type(img_obj)}")
            
            itk_image = img_obj[0] if isinstance(img_obj, tuple) else img_obj
            shape = itk_image.GetLargestPossibleRegion().GetSize()
            
            if any(s < r for s, r in zip(shape, self.roi_size)):
                raise ValueError(f"Image size {shape} is smaller than ROI size {self.roi_size}")

            if not self.precomputed_positions:
                #shape = (shape[2], shape[1], shape[0])  # Convert to XYZ order
                self.precompute_positions(shape)
            
            patches = []
            uids = []
            for _ in range(self.num_patches):
                # print("Current position index",self.current_position_index)
                if self.current_position_index >= len(self.precomputed_positions):
                    shuffle(self.precomputed_positions)
                    self.current_position_index = 0

                start_x, start_y, start_z = self.precomputed_positions[self.current_position_index]
                self.current_position_index += 1
                
                uid = start_y + start_x * shape[1] + start_z * shape[0] * shape[1]
                print("UID",uid)
                uids.append(uid)
                
                extract_index = [int(start_x), int(start_y), int(start_z)]  # ITK ZYX order
                extract_size = [int(self.roi_size[0]), int(self.roi_size[1]), int(self.roi_size[2])]
                
                self.logger.info(f"Extracting patch at index {extract_index} with size {extract_size}")
                # self.logger.info(f"Image shape: {shape}")
                
                # InputImageType = type(itk_image)
                # OutputImageType = type(itk_image)
                # extract_filter = itk.ExtractImageFilter[InputImageType, OutputImageType].New()
                # extract_filter.SetInput(itk_image)
                # extract_region = itk.ImageRegion[3]()
                # extract_region.SetIndex(extract_index)
                # extract_region.SetSize(extract_size)
                # extract_filter.SetExtractionRegion(extract_region)
                # extract_filter.SetDirectionCollapseToSubmatrix()
                
                # extract_filter.Update()
                # patch_itk = extract_filter.GetOutput()
                # patch = itk.array_from_image(patch_itk)

                # print the extract index and size
                # print(f"Extract index: {extract_index}")
                
                numpy_image = itk.array_from_image(itk_image)
                # Image in X,Y,Z order)
                numpy_image = numpy_image.transpose(2, 1, 0)
                patch = numpy_image[extract_index[0]:extract_index[0] + extract_size[0],
                                    extract_index[1]:extract_index[1] + extract_size[1],
                                    extract_index[2]:extract_index[2] + extract_size[2]]
                
                if patch.ndim == 3:
                    patch = patch[np.newaxis, ...]
                
                patches.append(patch)
                self.logger.info(f"Patch shape: {patch.shape}")

            patches = np.concatenate(patches, axis=0)
            uids = torch.tensor(uids)
            data['image'] = patches  
            data['uids'] = uids  
            return data
        except Exception as e:
            self.logger.error(f"Error in LazyPatchLoader: {str(e)}")
            raise

# Set up logging
#logging.basicConfig(level=logging.INFO)

def count_parameters(model):
    return sum(p.numel() for p in model.parameters())



def main(loso_index):
    from sklearn.preprocessing import LabelEncoder
    device = get_device()
    labels = ['normal1', 'normal2', 'cyst1', 'cyst2', 'hemangioma', 'metastatsis']
    scanner_labels = ['A1', 'A2', 'B1', 'B2', 'C1', 'D1', 'E1', 'E2', 'F1', 'G1', 'G2', 'H1', 'H2']
    removed_scanner = scanner_labels[loso_index]
    scanner_labels.remove(removed_scanner)
    print("\033[91m" + f"List of scanners {scanner_labels}" + "\033[0m")
    encoder = LabelEncoder()
    encoder.fit(labels)
    scanner_encoder = LabelEncoder()
    scanner_encoder.fit(scanner_labels)
    transforms = Compose([
        #PrintDebug(),
        #Resized(keys=["image"],spatial_size = (512,512,343)),
        #LoadImaged(keys=["image"]),
        LazyPatchLoader(roi_size=[64, 64, 32]),
        #DebugTransform2(),
        EnsureChannelFirstd(keys=["image"], channel_dim="no_channel"),
        EnsureTyped(keys=["image"], device=device, track_meta=False),
        #EncodeLabels(encoder=encoder),
        ExtractScannerLabel(),
        EncodeLabels(encoder=scanner_encoder, key='scanner_label'),
        #DebugTransform(),
        #DebugTransform2(),
        
    ])


    jsonpath = "./train_configurations/liver_registered_light_dataset_info_10.json"
    #jsonpath = "./dataset_info_cropped.json"
    
    data_list_org = load_data(jsonpath)
    # Add a prefix to all elements in data_list_org
    dataset_prefix = "/mnt/nas7/data/Past-TeamMembers/jeanfelix_maestrati"
    data_list = []
    for item in data_list_org:
        if not removed_scanner in item['image']:
            item['image'] = os.path.join(dataset_prefix, item['image'].replace('./', ''))
            data_list.append(item)
    # print(data_list)
    train_data, test_data = create_datasets(data_list,test_size=0.00)
    model = get_model(target_size=(64, 64, 32))
    #model = get_oscar_for_training()
    #transfering model on device 
    model.to(device)
    
    number_of_workers = 1
    train_dataset = SmartCacheDataset(data=train_data, transform=transforms,cache_rate=1,progress=True,num_init_workers=number_of_workers, num_replace_workers=number_of_workers,replace_rate=0.1)
    test_dataset = SmartCacheDataset(data=test_data, transform=transforms,cache_rate=0.1,progress=True,num_init_workers=number_of_workers, num_replace_workers=number_of_workers)
    
    train_loader = ThreadDataLoader(train_dataset, batch_size=10, shuffle=True,collate_fn=custom_collate_fn)
    test_loader = ThreadDataLoader(test_dataset, batch_size=3, shuffle=False,collate_fn=custom_collate_fn)
    
    data_loader = {'train': train_loader, 'test': test_loader}
    dataset = {'train': train_dataset, 'test': test_dataset}    
    
    print(f"Le nombre total de poids dans le modèle est : {count_parameters(model)}")
    
    optimizer = optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0.005) 
    lr_scheduler = CosineAnnealingLR(optimizer, T_max=50, eta_min=1e-6)
    
    trainer = Train(model, data_loader, optimizer, lr_scheduler, 20, dataset, contrastive_latentsize=768,
                     savename=f'./checkpoints/liverrandom_contrast_5_15_10_batch_swin_loso_{loso_index :02d}.pth',ortho_reg=0.001)
    trainer.train()

def classify_cross_val(results, latents_t, labels_t, latents_v, labels_v, groups, lock):
    
    latents_t = np.array(latents_t)
    labels_t = np.array(labels_t)
    latents_v = np.array(latents_v)
    labels_v = np.array(labels_v)
    num_classes = 6
    labels_t = to_categorical(labels_t, num_classes=num_classes)
    labels_v = to_categorical(labels_v, num_classes=num_classes)
    it2 = range(1, 13)
    for N in it2:
        N = N / 12
        if N == 1:
            full_indices = np.arange(len(latents_t))
            it3 = [(full_indices, None)]
        else:
            splits = GroupShuffleSplit(n_splits=1, test_size=N, random_state=42)
            it3 = splits.split(latents_t, labels_t, groups=groups)
        for train_idx, _ in it3:
            
            print("Debugging Info:")
            print(f"Content of latents_t: {latents_t}")
            print(f"Contents of train_idx: {train_idx}")
            print(f"N: {N}")
            print(f"Type of train_idx: {type(train_idx)}")
            print(f"Shape of train_idx: {np.shape(train_idx)}")
            print(f"Shape of latents_t: {np.shape(latents_t)}")
            print(f"Type of latents_t: {type(latents_t)}")
            print(f"Shape of labels_t: {np.shape(labels_t)}")
            
            
            x_train = latents_t[train_idx,:]
            y_train = labels_t[train_idx]
            classifier = define_classifier(3072, num_classes)
                
            history = classifier.fit(
                x_train, y_train,
                validation_data=(latents_v, labels_v),
                batch_size=64,
                epochs=75,
                verbose=0,
            )
            max_val_accuracy = max(history.history['val_accuracy'])
            with lock:
                if N not in results:
                    results[N] = []
                results[N].append(max_val_accuracy)
            
            tf.keras.backend.clear_session()
            del x_train, y_train, classifier, history

def cross_val_training():
    from sklearn.preprocessing import LabelEncoder
    device = get_device()
    labels = ['normal1', 'normal2', 'cyst1', 'cyst2', 'hemangioma', 'metastatsis']
    scanner_labels = ['A1', 'A2', 'B1', 'B2', 'C1', 'D1', 'E1', 'E2', 'F1', 'G1', 'G2', 'H1', 'H2']
    encoder = LabelEncoder()
    encoder.fit(labels)
    scanner_encoder = LabelEncoder()
    scanner_encoder.fit(scanner_labels)
    transforms = Compose([
        #PrintDebug(),
        LoadImaged(keys=["image"]),
        #DebugTransform2(),
        EnsureChannelFirstd(keys=["image"]),
        EnsureTyped(keys=["image"], device=device, track_meta=False),
        #EncodeLabels(encoder=encoder),
        ExtractScannerLabel(),
        EncodeLabels(encoder=scanner_encoder, key='scanner_label'),
        #DebugTransform(),
        #DebugTransform2(),  
    ])

    jsonpath = "./dataset_info_cropped.json"
    from sklearn.model_selection import LeaveOneGroupOut
    
    data_list = load_data(jsonpath)
    ogroups = group_data(data_list, mode='scanner') 
    
    logo = LeaveOneGroupOut()
    it1 = enumerate(logo.split(data_list, groups=ogroups))
    results = {}
    results_lock = threading.Lock()
    
    def classify_cross_val_wrapper(latents_t, labels_t, latents_v, labels_v, groups):
        classify_cross_val(results, latents_t, labels_t, latents_v, labels_v, groups, results_lock)
    threads = []
    for _, (train_idx, test_idx) in tqdm(it1):
        
        print(f"The test index is {test_idx}")
        removed_groups = [ogroups[idx] for idx in test_idx]
        print(f"In the beginning We took out the groups {removed_groups}")
        print(f"In the beggining Unique groups are : {np.unique(ogroups)}")
        
        train_data = [data_list[i] for i in train_idx]
        test_data = [data_list[i] for i in test_idx]
        model = get_model(target_size=(64, 64, 32))
        train_dataset = SmartCacheDataset(data=train_data, transform=transforms,cache_rate=1,progress=True,num_init_workers=8, num_replace_workers=8,replace_rate=0.1)
        test_dataset = SmartCacheDataset(data=test_data, transform=transforms,cache_rate=0.15,progress=True,num_init_workers=8, num_replace_workers=8)
        
        train_loader = ThreadDataLoader(train_dataset, batch_size=32, shuffle=True,collate_fn=custom_collate_fn)
        test_loader = ThreadDataLoader(test_dataset, batch_size=12, shuffle=False,collate_fn=custom_collate_fn)
        
        data_loader = {'train': train_loader, 'test': test_loader}
        dataset = {'train': train_dataset, 'test': test_dataset}
    
        print(f"Le nombre total de poids dans le modèle est : {count_parameters(model)}")
        optimizer = optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0.005) #i didnt add the decoder params so they didnt get updated
        lr_scheduler = CosineAnnealingLR(optimizer, T_max=50, eta_min=1e-6)
        
        #with savename being related to the group out
        #search for a file with "paper" in the name and test_data[0]['info']['SeriesDescription'] in the name and use it to load a model if found, and train it if not
        
        #only search for names that doesnt contain "reconstruction" in them and that contains test_data[0]['info']['SeriesDescription']
        
        
        series_description = test_data[0]['info']['SeriesDescription']
        search_pattern = f"*paper*{series_description}*pth"
        file_list = glob.glob(search_pattern)
        
        # Filter out files that contain "reconstruction"
        filtered_files = [file for file in file_list if "reconstruction" not in file]
        
        if False:#filtered_files:
            print(f"Found a model with the name {filtered_files[0]}")
            model.load_state_dict(torch.load(filtered_files[0]))
            print(f"Model loaded from {filtered_files[0]}")
            
            latents_t = []
            labels_t = []
            latents_v = []
            labels_v = []
            groups = []
            with torch.no_grad():
                for batch in data_loader['train']:
                    images = batch['image'].cuda()
                    latents_tensor = model.swinViT(images)[4]
                    
                    batch_size, channels, *dims = latents_tensor.size()
                    flatten_size = torch.prod(torch.tensor(dims)).item()
                    
                    latents_tensor = latents_tensor.reshape(batch_size, channels * flatten_size)
                    latents_t.extend(latents_tensor.cpu().numpy())
                    labels_t.extend(batch['roi_label'].cpu().numpy()) 
                    groups.extend(batch['scanner_label'].cpu().numpy())
                    del images
                    torch.cuda.empty_cache()
                
                for batch in data_loader['test']:
                    images = batch['image'].cuda()
                    latents_tensor = model.swinViT(images)[4]
                    
                    batch_size, channels, *dims = latents_tensor.size()
                    flatten_size = torch.prod(torch.tensor(dims)).item()
                    
                    latents_tensor = latents_tensor.reshape(batch_size, channels * flatten_size)
                    latents_v.extend(latents_tensor.cpu().numpy())
                    labels_v.extend(batch['roi_label'].cpu().numpy())
                    del images
                    torch.cuda.empty_cache()
                    
            dataset['train'].shutdown()
            dataset['test'].shutdown()
        else:
            trainer = Train(model, data_loader, optimizer, lr_scheduler, 9,dataset,contrastive_latentsize=768,savename=f"paper_contrastive_{test_data[0]['info']['SeriesDescription']}.pth")
            latents_t,labels_t,latents_v,labels_v,groups = trainer.train()
        
        print(f"Finished training for group {test_data[0]['info']['SeriesDescription']}")
        unique_groups = np.unique(groups)
        #print(f"In the end We took out the group {groups[test_idx[0]]}")
        print(f"In the end Unique groups are : {unique_groups} and their number is {len(unique_groups)}")
        
        #classifiy
        
        thread = threading.Thread(target=classify_cross_val_wrapper, args=(latents_t, labels_t, latents_v, labels_v, groups))
        thread.start()
        threads.append(thread)
        print("Shape of latents_t:", np.shape(latents_t))
        #classify_cross_val(results, latents_t, labels_t, latents_v, labels_v, groups, results_lock)
        
        #printing results
        with results_lock:
            print("Results so far:")
            for key, values in results.items():
                print(f"Test size: {key}, Accuracies: {values}")
        
        print("On va saaaveeeeee")
        
        save_results_to_csv(results, classif_type="roi_large", mg_filter=None, data_path="./swinunetr_paper.json",plus="crossval_trained")        
        
    
    for thread in threads:
        thread.join()
    
    save_results_to_csv(results, classif_type="roi_large", mg_filter=None, data_path="./swinunetr_paper.json",plus="crossval_trained")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train a contrastive learning based model.')
    parser.add_argument('--loso_index', type=int, default=0)
    
    args = parser.parse_args()
    print("\033[91m" + f"LOSO index is {args.loso_index}" + "\033[0m")
    time.sleep(5)
    
    main(args.loso_index)
