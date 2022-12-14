import torch
import torch.nn as nn
import numpy as np
import torchgeometry as tgm
from torch.utils.data import DataLoader
from tqdm import tqdm
import json
import smplx
import os

from dataset import EgoSetDataset
from models import hmr, SMPL
from utils.geometry import batch_rodrigues, perspective_projection, estimate_translation
from utils.renderer import Renderer
from utils.pose_utils import reconstruction_error
from utils import BaseTrainer
from utils import TrainOptions
import config
import constants


def weakProjection_gpu(skel3D, scale, trans2D):
    skel3D = skel3D.view((skel3D.shape[0], -1, 3))
    trans2D = trans2D.view((trans2D.shape[0] , 1, 2))
    scale = scale.view((scale.shape[0], 1 , 1))
    skel3D_proj = scale*skel3D[:,:,:2] + trans2D
    return skel3D_proj


class Trainer(BaseTrainer):
    
    def init_fn(self):
        self.train_ds = EgoSetDataset(self.options, use_augmentation=self.options.use_aug, is_train=True, period='train')
        self.test_ds = EgoSetDataset(self.options, use_augmentation=False, is_train=False, period='test')

        print('train: ', len(self.train_ds))
        print('test: ', len(self.test_ds))

        # data = self.train_ds[0]
        # for key in data.keys():
        #     try:
        #         print(key, ':', data[key].shape)
        #     except:
        #         print(key, ':', data[key])

        self.model = hmr(config.SMPL_MEAN_PARAMS, pretrained=True).to(self.device)
        self.optimizer = torch.optim.Adam(params=self.model.parameters(),
                                          lr=self.options.lr,
                                          weight_decay=0)
        self.smpl = SMPL(config.SMPL_MODEL_DIR,
                         batch_size=self.options.batch_size,
                         create_transl=False).to(self.device)
        self.smpl_eval = smplx.create(model_path=config.OTHER_DATA_ROOT,
                                      model_type='smpl',
                                      create_transl=False).to(self.device)
                         
        # Per-vertex loss on the shape
        self.criterion_shape = nn.L1Loss().to(self.device)
        # Keypoint (2D and 3D) loss
        # No reduction because confidence weighting needs to be applied
        self.criterion_keypoints = nn.MSELoss(reduction='none').to(self.device)
        # Loss for SMPL parameter regression
        self.criterion_regr = nn.MSELoss().to(self.device)
        self.models_dict = {'model': self.model}
        self.optimizers_dict = {'optimizer': self.optimizer}
        self.focal_length = constants.FOCAL_LENGTH

        if self.options.pretrained_checkpoint is not None:
            self.load_pretrained(checkpoint_file=self.options.pretrained_checkpoint)

        # Create renderer
        self.renderer = Renderer(focal_length=self.focal_length, img_res=self.options.img_res, faces=self.smpl.faces)

    def finalize(self):
        self.fits_dict.save()

    def keypoint_loss(self, pred_keypoints_2d, gt_keypoints_2d, openpose_weight, gt_weight):
        """ Compute 2D reprojection loss on the keypoints.
        The loss is weighted by the confidence.
        The available keypoints are different for each dataset.
        """
        conf = gt_keypoints_2d[:, :, -1].unsqueeze(-1).clone()
        conf[:, :25] *= openpose_weight
        conf[:, 25:] *= gt_weight
        loss = (conf * self.criterion_keypoints(pred_keypoints_2d, gt_keypoints_2d[:, :, :-1])).mean()
        return loss

    def keypoint_3d_loss(self, pred_keypoints_3d, gt_keypoints_3d, has_pose_3d):
        """Compute 3D keypoint loss for the examples that 3D keypoint annotations are available.
        The loss is weighted by the confidence.
        """
        pred_keypoints_3d = pred_keypoints_3d[:, 25:, :]
        conf = gt_keypoints_3d[:, :, -1].unsqueeze(-1).clone()
        gt_keypoints_3d = gt_keypoints_3d[:, :, :-1].clone()
        gt_keypoints_3d = gt_keypoints_3d[has_pose_3d == 1]
        conf = conf[has_pose_3d == 1]
        pred_keypoints_3d = pred_keypoints_3d[has_pose_3d == 1]
        if len(gt_keypoints_3d) > 0:
            gt_pelvis = (gt_keypoints_3d[:, 2,:] + gt_keypoints_3d[:, 3,:]) / 2
            gt_keypoints_3d = gt_keypoints_3d - gt_pelvis[:, None, :]
            pred_pelvis = (pred_keypoints_3d[:, 2,:] + pred_keypoints_3d[:, 3,:]) / 2
            pred_keypoints_3d = pred_keypoints_3d - pred_pelvis[:, None, :]
            return (conf * self.criterion_keypoints(pred_keypoints_3d, gt_keypoints_3d)).mean()
        else:
            return torch.FloatTensor(1).fill_(0.).to(self.device)

    def shape_loss(self, pred_vertices, gt_vertices, has_smpl):
        """Compute per-vertex loss on the shape for the examples that SMPL annotations are available."""
        pred_vertices_with_shape = pred_vertices[has_smpl == 1]
        gt_vertices_with_shape = gt_vertices[has_smpl == 1]
        if len(gt_vertices_with_shape) > 0:
            return self.criterion_shape(pred_vertices_with_shape, gt_vertices_with_shape)
        else:
            return torch.FloatTensor(1).fill_(0.).to(self.device)

    def smpl_losses(self, pred_rotmat, pred_betas, gt_pose, gt_betas, has_smpl):
        pred_rotmat_valid = pred_rotmat[has_smpl == 1]
        gt_rotmat_valid = batch_rodrigues(gt_pose.view(-1,3)).view(-1, 24, 3, 3)[has_smpl == 1]
        pred_betas_valid = pred_betas[has_smpl == 1]
        gt_betas_valid = gt_betas[has_smpl == 1]
        if len(pred_rotmat_valid) > 0:
            loss_regr_pose = self.criterion_regr(pred_rotmat_valid, gt_rotmat_valid)
            loss_regr_betas = self.criterion_regr(pred_betas_valid, gt_betas_valid)
        else:
            loss_regr_pose = torch.FloatTensor(1).fill_(0.).to(self.device)
            loss_regr_betas = torch.FloatTensor(1).fill_(0.).to(self.device)
        return loss_regr_pose, loss_regr_betas

    def train_step(self, input_batch):
        self.model.train()

        # Get data from the batch
        images = input_batch['img'] # input image
        gt_keypoints_2d = input_batch['keypoints'] # 2D keypoints
        # gt_trans = input_batch['trans']
        gt_pose = input_batch['pose'] # SMPL pose parameters
        gt_betas = input_batch['betas'] # SMPL beta parameters
        gt_joints = input_batch['pose_3d'] # 3D pose
        has_smpl = input_batch['has_smpl'].byte() # flag that indicates whether SMPL parameters are valid
        has_pose_3d = input_batch['has_pose_3d'].byte() # flag that indicates whether 3D pose is valid
        is_flipped = input_batch['is_flipped'] # flag that indicates whether image was flipped during data augmentation
        rot_angle = input_batch['rot_angle'] # rotation angle used for data augmentation
        dataset_name = input_batch['dataset_name'] # name of the dataset the image comes from
        indices = input_batch['sample_index'] # index of example inside its dataset
        batch_size = images.shape[0]

        # Get GT vertices and model joints
        # Note that gt_model_joints is different from gt_joints as it comes from SMPL
        gt_out = self.smpl(betas=gt_betas, 
                           body_pose=gt_pose[:,3:], 
                           global_orient=gt_pose[:,:3])
        gt_model_joints = gt_out.joints
        gt_vertices = gt_out.vertices

        # De-normalize 2D keypoints from [-1,1] to pixel space
        gt_keypoints_2d_orig = gt_keypoints_2d.clone()
        gt_keypoints_2d_orig[:, :, :-1] = 0.5 * self.options.img_res * (gt_keypoints_2d_orig[:, :, :-1] + 1)

        # Estimate camera translation given the model joints and 2D keypoints
        # by minimizing a weighted least squares loss
        try:
            gt_cam_t = estimate_translation(gt_model_joints, gt_keypoints_2d_orig, focal_length=self.focal_length, img_size=self.options.img_res)
        except:
            gt_cam_t = None

        # Feed images in the network to predict camera and SMPL parameters
        pred_rotmat, pred_betas, pred_camera = self.model(images)
        pred_output = self.smpl(betas=pred_betas, 
                                body_pose=pred_rotmat[:,1:], 
                                global_orient=pred_rotmat[:,0].unsqueeze(1), 
                                pose2rot=False)
        pred_vertices = pred_output.vertices
        pred_joints = pred_output.joints

        if self.options.use_wpp:
            pred_keypoints_2d = weakProjection_gpu(pred_joints, 
                                                   pred_camera[:,0], 
                                                   pred_camera[:,1:])
            pred_cam_t = torch.zeros(batch_size, 3, device=self.device)
        else:
            # Convert Weak Perspective Camera [s, tx, ty] to camera translation [tx, ty, tz] in 3D given the bounding box size
            # This camera translation can be used in a full perspective projection
            pred_cam_t = torch.stack([pred_camera[:,1],
                                      pred_camera[:,2],
                                      2*self.focal_length / (self.options.img_res*pred_camera[:,0]+1e-9)], 
                                     dim=-1)
            camera_center = torch.zeros(batch_size, 2, device=self.device)
            # if use following line, the normalize step should be different
            # camera_center[:, :] = self.options.img_res/2
            # world -> screen
            pred_keypoints_2d = perspective_projection(pred_joints,
                                                       rotation=torch.eye(3, device=self.device).unsqueeze(0).expand(batch_size, -1, -1),
                                                       translation=pred_cam_t,
                                                       focal_length=self.focal_length,
                                                       camera_center=camera_center)
        if gt_cam_t is None:
                gt_cam_t = pred_cam_t.clone().detach()
        # Normalize keypoints to [-1, 1]
        pred_keypoints_2d = pred_keypoints_2d / (self.options.img_res / 2.)

        valid_fit = has_smpl.bool()

        # Compute loss on SMPL parameters
        loss_regr_pose, loss_regr_betas = self.smpl_losses(pred_rotmat, pred_betas, gt_pose, gt_betas, valid_fit)

        # Compute 2D reprojection loss for the keypoints
        loss_keypoints = self.keypoint_loss(pred_keypoints_2d, 
                                            gt_keypoints_2d,
                                            self.options.openpose_train_weight,
                                            self.options.gt_train_weight)

        # Compute 3D keypoint loss
        loss_keypoints_3d = self.keypoint_3d_loss(pred_joints, gt_joints, has_pose_3d)

        # Per-vertex loss for the shape
        # loss_shape = self.shape_loss(pred_vertices, opt_vertices, valid_fit)
        loss_shape = self.shape_loss(pred_vertices, gt_vertices, valid_fit)

        # Compute total loss
        # The last component is a loss that forces the network to predict positive depth values
        loss = self.options.shape_loss_weight * loss_shape +\
               self.options.keypoint_loss_weight * loss_keypoints +\
               self.options.keypoint_loss_weight * loss_keypoints_3d +\
               self.options.pose_loss_weight * loss_regr_pose +\
               self.options.beta_loss_weight * loss_regr_betas +\
               ((torch.exp(-pred_camera[:,0]*10)) ** 2 ).mean()
        loss *= 60

        # Do backprop
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        output = {'pred_vertices': pred_vertices.detach(),
                  'opt_vertices': gt_vertices,
                  'pred_cam_t': pred_cam_t.detach(),
                  'opt_cam_t': gt_cam_t}
        losses = {'loss': loss.detach().item(),
                  'loss_keypoints': loss_keypoints.detach().item(),
                  'loss_keypoints_3d': loss_keypoints_3d.detach().item(),
                  'loss_regr_pose': loss_regr_pose.detach().item(),
                  'loss_regr_betas': loss_regr_betas.detach().item(),
                  'loss_shape': loss_shape.detach().item()}

        return output, losses

    def train_summaries(self, input_batch, output, losses):
        images = input_batch['img']
        images = images * torch.tensor([0.229, 0.224, 0.225], device=images.device).reshape(1,3,1,1)
        images = images + torch.tensor([0.485, 0.456, 0.406], device=images.device).reshape(1,3,1,1)
        if not self.options.use_wpp:
            pred_vertices = output['pred_vertices']
            opt_vertices = output['opt_vertices']
            pred_cam_t = output['pred_cam_t']
            opt_cam_t = output['opt_cam_t']
            images_pred = self.renderer.visualize_tb(pred_vertices, pred_cam_t, images)
            images_opt = self.renderer.visualize_tb(opt_vertices, opt_cam_t, images)
            self.summary_writer.add_image('pred_shape', images_pred, self.step_count)
            self.summary_writer.add_image('opt_shape', images_opt, self.step_count)             
        for loss_name, val in losses.items():
            self.summary_writer.add_scalar(loss_name, val, self.step_count)

    def test_step(self, input_batch):
        self.model.eval()

        images = input_batch['img'] 
        gt_keypoints_2d = input_batch['keypoints'] 
        # gt_trans = input_batch['trans']
        gt_pose = input_batch['pose'] 
        gt_betas = input_batch['betas'] 
        has_smpl = input_batch['has_smpl'].byte() 
        batch_size = images.shape[0]

        gt_out = self.smpl(betas=gt_betas, 
                           body_pose=gt_pose[:,3:], 
                           global_orient=gt_pose[:,:3])
        gt_model_joints = gt_out.joints
        gt_vertices = gt_out.vertices
    
        gt_keypoints_2d_orig = gt_keypoints_2d.clone()
        gt_keypoints_2d_orig[:, :, :-1] = 0.5 * self.options.img_res * (gt_keypoints_2d_orig[:, :, :-1] + 1)

        pred_rotmat, pred_betas, pred_camera = self.model(images)
        pred_output = self.smpl(betas=pred_betas, 
                                body_pose=pred_rotmat[:,1:], 
                                global_orient=pred_rotmat[:,0].unsqueeze(1), 
                                pose2rot=False)
        pred_vertices = pred_output.vertices
        pred_joints = pred_output.joints

        if self.options.use_wpp:
            pred_keypoints_2d = weakProjection_gpu(pred_joints, 
                                                   pred_camera[:,0], 
                                                   pred_camera[:,1:])
        else:
            pred_cam_t = torch.stack([pred_camera[:,1],
                                      pred_camera[:,2],
                                      2*self.focal_length / (self.options.img_res*pred_camera[:,0]+1e-9)], 
                                     dim=-1)
            camera_center = torch.zeros(batch_size, 2, device=self.device)
            pred_keypoints_2d = perspective_projection(pred_joints,
                                                       rotation=torch.eye(3, device=self.device).unsqueeze(0).expand(batch_size, -1, -1),
                                                       translation=pred_cam_t,
                                                       focal_length=self.focal_length,
                                                       camera_center=camera_center)
        pred_keypoints_2d = pred_keypoints_2d / (self.options.img_res / 2.)

        valid_fit = has_smpl

        loss_regr_pose, loss_regr_betas = self.smpl_losses(pred_rotmat, pred_betas, gt_pose, gt_betas, valid_fit)

        loss_keypoints = self.keypoint_loss(pred_keypoints_2d, 
                                            gt_keypoints_2d,
                                            self.options.openpose_train_weight,
                                            self.options.gt_train_weight)

        loss_shape = self.shape_loss(pred_vertices, gt_vertices, valid_fit)

        loss = self.options.shape_loss_weight * loss_shape +\
               self.options.keypoint_loss_weight * loss_keypoints +\
               self.options.pose_loss_weight * loss_regr_pose +\
               self.options.beta_loss_weight * loss_regr_betas +\
               ((torch.exp(-pred_camera[:,0]*10)) ** 2 ).mean()
        loss *= 60

        losses = {'loss_test': loss.detach().item(),
                  'loss_keypoints_test': loss_keypoints.detach().item(),
                  'loss_regr_pose_test': loss_regr_pose.detach().item(),
                  'loss_regr_betas_test': loss_regr_betas.detach().item(),
                  'loss_shape_test': loss_shape.detach().item()}
        
        return losses

    def test_summaries(self, losses):
        for loss_name, val in losses.items():
            self.summary_writer.add_scalar(loss_name, val, self.step_count)

    def eval(self):
        self.model.eval()

        batch_size = self.options.batch_size

        data_loader = DataLoader(self.test_ds, 
                                 batch_size=batch_size,
                                 num_workers=self.options.num_workers,
                                 shuffle=False)

        mpjpe = np.zeros(len(self.test_ds))
        pa_mpjpe = np.zeros(len(self.test_ds))
        v2v = np.zeros(len(self.test_ds))
        pa_v2v = np.zeros(len(self.test_ds))

        pred_dict = {}

        for step, batch in enumerate(tqdm(data_loader, desc='Eval', total=len(data_loader))):
            # Get ground truth annotations from the batch
            gt_pose = batch['pose'].to(self.device)
            gt_betas = batch['betas'].to(self.device)
            gt_output = self.smpl_eval(betas=gt_betas, 
                                       body_pose=gt_pose[:, 3:], 
                                       global_orient=gt_pose[:, :3])
            gt_joints = gt_output.joints[:, :24]
            gt_vertices = gt_output.vertices

            images = batch['img'].to(self.device)
            curr_batch_size = images.shape[0]

            with torch.no_grad():
                pred_rotmat, pred_betas, pred_camera = self.model(images)
                pred_output = self.smpl_eval(betas=pred_betas, 
                                             body_pose=pred_rotmat[:,1:], 
                                             global_orient=pred_rotmat[:,0].unsqueeze(1), pose2rot=False)
                pred_joints = pred_output.joints[:, :24]
                pred_vertices = pred_output.vertices

            mpjpe[step*batch_size:step*batch_size+curr_batch_size] = torch.sqrt(((pred_joints - gt_joints)**2).sum(dim=-1)).mean(dim=-1).cpu().numpy()

            pa_mpjpe[step*batch_size:step*batch_size+curr_batch_size] = reconstruction_error(pred_joints.cpu().numpy(), gt_joints.cpu().numpy(), reduction=None)
            
            v2v[step*batch_size:step*batch_size+curr_batch_size] = torch.sqrt(((pred_vertices - gt_vertices)**2).sum(dim=-1)).mean(dim=-1).cpu().numpy()

            pa_v2v[step*batch_size:step*batch_size+curr_batch_size] = reconstruction_error(pred_vertices.cpu().numpy(), gt_vertices.cpu().numpy(), reduction=None)

            # save for submission
            rot_pad = torch.tensor([0,0,1], dtype=torch.float32, device=self.device).view(1,3,1)
            rotmat = torch.cat((pred_rotmat.view(-1, 3, 3), rot_pad.expand(curr_batch_size * 24, -1, -1)), dim=-1)
            pred_pose = tgm.rotation_matrix_to_angle_axis(rotmat).contiguous().view(-1, 72)
            img_path = batch['imgname']
            for i in range(curr_batch_size):
                recording_name = img_path[i].split('/')[-4]
                frame_name = img_path[i].split('/')[-1]
                if recording_name not in pred_dict.keys():
                    pred_dict[recording_name] = {}
                pred_dict[recording_name][frame_name] = {
                    'gender': 'neutral',
                    'body_pose': [pred_pose[i][3:].cpu().numpy().tolist()],
                    'global_orient': [pred_pose[i][:3].cpu().numpy().tolist()],
                    'betas': [pred_betas[i].cpu().numpy().tolist()]
                }

        print(self.submission_path)
        print('MPJPE: ' + str(1000 * mpjpe.mean()))
        print('PA-MPJPE: ' + str(1000 * pa_mpjpe.mean()))
        print('V2V: ' + str(1000 * v2v.mean()))
        print('PA-V2V: ' + str(1000 * pa_v2v.mean()))

        with open(self.eval_result_path, 'a+') as f:
            f.writelines(self.submission_path + '\n')
            f.writelines('MPJPE: ' + str(1000 * mpjpe.mean()) + '\n')
            f.writelines('PA-MPJPE: ' + str(1000 * pa_mpjpe.mean()) + '\n')
            f.writelines('V2V: ' + str(1000 * v2v.mean()) + '\n')
            f.writelines('PA-V2V: ' + str(1000 * pa_v2v.mean()) + '\n')
            f.writelines('\n')

        pred_json=json.dumps(pred_dict)
        with open(self.submission_path, 'w+') as f:
            f.write(pred_json)
        return


if __name__ == '__main__':
    options = TrainOptions().parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = options.device_id
    trainer = Trainer(options)
    trainer.train()