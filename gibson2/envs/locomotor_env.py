from gibson2.core.physics.interactive_objects import VisualObject, InteractiveObj
import gibson2
from gibson2.utils.utils import parse_config, rotate_vector_3d, l2_distance, quatToXYZW
from gibson2.envs.base_env import BaseEnv
from transforms3d.euler import euler2quat
from collections import OrderedDict
import argparse
from gibson2.learn.completion import CompletionNet, identity_init, Perceptual
import torch.nn as nn
import torch
from torchvision import datasets, transforms
from transforms3d.quaternions import quat2mat, qmult
import gym
import numpy as np
import os
import pybullet as p
from IPython import embed

# define navigation environments following Anderson, Peter, et al. 'On evaluation of embodied navigation agents.'
# arXiv preprint arXiv:1807.06757 (2018).
# https://arxiv.org/pdf/1807.06757.pdf


class NavigateEnv(BaseEnv):
    def __init__(
            self,
            config_file,
            mode='headless',
            action_timestep=1 / 10.0,
            physics_timestep=1 / 240.0,
            automatic_reset=False,
            device_idx=0,
    ):
        super(NavigateEnv, self).__init__(config_file=config_file, mode=mode, device_idx=device_idx)
        self.automatic_reset = automatic_reset

        # simulation
        self.mode = mode
        self.action_timestep = action_timestep
        self.physics_timestep = physics_timestep
        self.simulator.set_timestep(physics_timestep)
        self.simulator_loop = int(self.action_timestep / self.simulator.timestep)
        # self.reward_stats = []
        # self.state_stats = {'sensor': [], 'auxiliary_sensor': []}

    def load(self):
        super(NavigateEnv, self).load()
        self.initial_pos = np.array(self.config.get('initial_pos', [0, 0, 0]))
        self.initial_orn = np.array(self.config.get('initial_orn', [0, 0, 0]))

        self.target_pos = np.array(self.config.get('target_pos', [5, 5, 0]))
        self.target_orn = np.array(self.config.get('target_orn', [0, 0, 0]))

        self.additional_states_dim = self.config.get('additional_states_dim', 0)
        self.auxiliary_sensor_dim = self.config.get('auxiliary_sensor_dim', 0)
        self.normalize_observation = self.config.get('normalize_observation', False)
        self.observation_normalizer = self.config.get('observation_normalizer', {})
        for key in self.observation_normalizer:
            self.observation_normalizer[key] = np.array(self.observation_normalizer[key])

        # termination condition
        self.dist_tol = self.config.get('dist_tol', 0.2)
        self.max_step = self.config.get('max_step', float('inf'))

        # reward
        self.success_reward = self.config.get('success_reward', 10.0)
        self.slack_reward = self.config.get('slack_reward', -0.01)

        # reward weight
        self.potential_reward_weight = self.config.get('potential_reward_weight', 10.0)
        self.electricity_reward_weight = self.config.get('electricity_reward_weight', 0.0)
        self.stall_torque_reward_weight = self.config.get('stall_torque_reward_weight', 0.0)
        self.collision_reward_weight = self.config.get('collision_reward_weight', 0.0)
        self.collision_links = set(self.config.get('collision_links', [-1]))

        # discount factor
        self.discount_factor = self.config.get('discount_factor', 1.0)
        self.output = self.config['output']

        self.sensor_dim = self.additional_states_dim
        self.action_dim = self.robots[0].action_dim

        observation_space = OrderedDict()
        if 'sensor' in self.output:
            self.sensor_space = gym.spaces.Box(low=-np.inf,
                                               high=np.inf,
                                               shape=(self.sensor_dim,),
                                               dtype=np.float32)
            observation_space['sensor'] = self.sensor_space
        if 'auxiliary_sensor' in self.output:
            self.auxiliary_sensor_space = gym.spaces.Box(low=-np.inf,
                                                         high=np.inf,
                                                         shape=(self.auxiliary_sensor_dim,),
                                                         dtype=np.float32)
            observation_space['auxiliary_sensor'] = self.auxiliary_sensor_space
        if 'pointgoal' in self.output:
            self.pointgoal_space = gym.spaces.Box(low=-np.inf,
                                                  high=np.inf,
                                                  shape=(2,),
                                                  dtype=np.float32)
            observation_space['pointgoal'] = self.pointgoal_space
        if 'rgb' in self.output:
            self.rgb_space = gym.spaces.Box(low=0.0,
                                            high=1.0,
                                            shape=(self.config['resolution'],
                                                   self.config['resolution'], 3),
                                            dtype=np.float32)
            observation_space['rgb'] = self.rgb_space
        if 'depth' in self.output:
            self.depth_space = gym.spaces.Box(low=0.0,
                                              high=1.0,
                                              shape=(self.config['resolution'],
                                                     self.config['resolution'], 1),
                                              dtype=np.float32)
            observation_space['depth'] = self.depth_space
        if 'rgb_filled' in self.output:  # use filler
            self.comp = CompletionNet(norm=nn.BatchNorm2d, nf=64)
            self.comp = torch.nn.DataParallel(self.comp).cuda()
            self.comp.load_state_dict(
                torch.load(os.path.join(gibson2.assets_path, 'networks', 'model.pth')))
            self.comp.eval()

        self.observation_space = gym.spaces.Dict(observation_space)
        self.action_space = self.robots[0].action_space

        # variable initialization
        self.current_episode = 0

        # add visual objects
        self.visual_object_at_initial_target_pos = self.config.get(
            'visual_object_at_initial_target_pos', False)

        if self.visual_object_at_initial_target_pos:
            self.initial_pos_vis_obj = VisualObject(rgba_color=[1, 0, 0, 0.5])
            self.target_pos_vis_obj = VisualObject(rgba_color=[0, 0, 1, 0.5])
            self.initial_pos_vis_obj.load()
            if self.config.get('target_visual_object_visible_to_agent', False):
                self.simulator.import_object(self.target_pos_vis_obj)
            else:
                self.target_pos_vis_obj.load()

    def reload(self, config_file):
        super(NavigateEnv, self).reload(config_file)
        self.initial_pos = np.array(self.config.get('initial_pos', [0, 0, 0]))
        self.initial_orn = np.array(self.config.get('initial_orn', [0, 0, 0]))

        self.target_pos = np.array(self.config.get('target_pos', [5, 5, 0]))
        self.target_orn = np.array(self.config.get('target_orn', [0, 0, 0]))

        self.additional_states_dim = self.config.get('additional_states_dim', 0)
        self.auxiliary_sensor_dim = self.config.get('auxiliary_sensor_dim', 0)
        self.normalize_observation = self.config.get('normalize_observation', False)
        self.observation_normalizer = self.config.get('observation_normalizer', {})
        for key in self.observation_normalizer:
            self.observation_normalizer[key] = np.array(self.observation_normalizer[key])

        # termination condition
        self.dist_tol = self.config.get('dist_tol', 0.5)
        self.max_step = self.config.get('max_step', float('inf'))

        # reward
        self.terminal_reward = self.config.get('terminal_reward', 0.0)
        self.electricity_cost = self.config.get('electricity_cost', 0.0)
        self.stall_torque_cost = self.config.get('stall_torque_cost', 0.0)
        self.collision_cost = self.config.get('collision_cost', 0.0)
        self.discount_factor = self.config.get('discount_factor', 1.0)
        self.output = self.config['output']

        self.sensor_dim = self.additional_states_dim
        self.action_dim = self.robots[0].action_dim

        # self.observation_space = gym.spaces.Box(low=-np.inf, high=np.inf, shape=(self.sensor_dim,), dtype=np.float64)
        observation_space = OrderedDict()
        if 'sensor' in self.output:
            self.sensor_space = gym.spaces.Box(low=-np.inf,
                                               high=np.inf,
                                               shape=(self.sensor_dim,),
                                               dtype=np.float32)
            observation_space['sensor'] = self.sensor_space
        if 'auxiliary_sensor' in self.output:
            self.auxiliary_sensor_space = gym.spaces.Box(low=-np.inf,
                                                         high=np.inf,
                                                         shape=(self.auxiliary_sensor_dim,),
                                                         dtype=np.float32)
            observation_space['auxiliary_sensor'] = self.auxiliary_sensor_space
        if 'rgb' in self.output:
            self.rgb_space = gym.spaces.Box(low=0.0,
                                            high=1.0,
                                            shape=(self.config['resolution'],
                                                   self.config['resolution'], 3),
                                            dtype=np.float32)
            observation_space['rgb'] = self.rgb_space
        if 'depth' in self.output:
            self.depth_space = gym.spaces.Box(low=0.0,
                                              high=1.0,
                                              shape=(self.config['resolution'],
                                                     self.config['resolution'], 1),
                                              dtype=np.float32)
            observation_space['depth'] = self.depth_space
        if 'rgb_filled' in self.output:  # use filler
            self.comp = CompletionNet(norm=nn.BatchNorm2d, nf=64)
            self.comp = torch.nn.DataParallel(self.comp).cuda()
            self.comp.load_state_dict(
                torch.load(os.path.join(gibson2.assets_path, 'networks', 'model.pth')))
            self.comp.eval()
        if 'pointgoal' in self.output:
            observation_space['pointgoal'] = gym.spaces.Box(low=-np.inf,
                                                            high=np.inf,
                                                            shape=(2,),
                                                            dtype=np.float32)

        self.observation_space = gym.spaces.Dict(observation_space)
        self.action_space = self.robots[0].action_space

        self.visual_object_at_initial_target_pos = self.config.get(
            'visual_object_at_initial_target_pos', False)
        if self.visual_object_at_initial_target_pos:
            self.initial_pos_vis_obj = VisualObject(rgba_color=[1, 0, 0, 0.5])
            self.target_pos_vis_obj = VisualObject(rgba_color=[0, 0, 1, 0.5])
            self.initial_pos_vis_obj.load()
            if self.config.get('target_visual_object_visible_to_agent', False):
                self.simulator.import_object(self.target_pos_vis_obj)
            else:
                self.target_pos_vis_obj.load()

    def get_additional_states(self):
        relative_position = self.target_pos - self.robots[0].get_position()
        # rotate relative position back to body point of view
        additional_states = rotate_vector_3d(relative_position, *self.robots[0].get_rpy())

        if self.config['task'] == 'reaching':
            end_effector_pos = self.robots[0].get_end_effector_position(
            ) - self.robots[0].get_position()
            end_effector_pos = rotate_vector_3d(end_effector_pos, *self.robots[0].get_rpy())
            additional_states = np.concatenate((additional_states, end_effector_pos))
        assert len(additional_states) == self.additional_states_dim, 'additional states dimension mismatch'

        return additional_states
        """
        relative_position = self.target_pos - self.robots[0].get_position()
        # rotate relative position back to body point of view
        relative_position_odom = rotate_vector_3d(relative_position, *self.robots[0].get_rpy())
        # the angle between the direction the agent is facing and the direction to the target position
        delta_yaw = np.arctan2(relative_position_odom[1], relative_position_odom[0])
        additional_states = np.concatenate((relative_position,
                                            relative_position_odom,
                                            [np.sin(delta_yaw), np.cos(delta_yaw)]))
        if self.config['task'] == 'reaching':
            # get end effector information

            end_effector_pos = self.robots[0].get_end_effector_position() - self.robots[0].get_position()
            end_effector_pos = rotate_vector_3d(end_effector_pos, *self.robots[0].get_rpy())
            additional_states = np.concatenate((additional_states, end_effector_pos))

        assert len(additional_states) == self.additional_states_dim, 'additional states dimension mismatch'
        return additional_states
        """

    def get_auxiliary_sensor(self):
        raise np.array([])

    def get_state(self, collision_links=[]):
        # calculate state
        # sensor_state = self.robots[0].calc_state()
        # sensor_state = np.concatenate((sensor_state, self.get_additional_states()))
        sensor_state = self.get_additional_states()
        auxiliary_sensor = self.get_auxiliary_sensor()

        state = OrderedDict()
        if 'sensor' in self.output:
            state['sensor'] = sensor_state
        if 'auxiliary_sensor' in self.output:
            state['auxiliary_sensor'] = auxiliary_sensor
        if 'pointgoal' in self.output:
            state['pointgoal'] = sensor_state[:2]
        if 'rgb' in self.output:
            state['rgb'] = self.simulator.renderer.render_robot_cameras(modes=('rgb'))[0][:, :, :3]
        if 'depth' in self.output:
            depth = -self.simulator.renderer.render_robot_cameras(modes=('3d'))[0][:, :, 2:3]
            state['depth'] = depth
        if 'normal' in self.output:
            state['normal'] = self.simulator.renderer.render_robot_cameras(modes='normal')
        if 'seg' in self.output:
            state['seg'] = self.simulator.renderer.render_robot_cameras(modes='seg')
        if 'rgb_filled' in self.output:
            with torch.no_grad():
                tensor = transforms.ToTensor()((state['rgb'] * 255).astype(np.uint8)).cuda()
                rgb_filled = self.comp(tensor[None, :, :, :])[0].permute(1, 2, 0).cpu().numpy()
                state['rgb_filled'] = rgb_filled
        if 'bump' in self.output:
            state['bump'] = -1 in collision_links  # check collision for baselink, it might vary for diauxiliary_sensorsfferent robots

        if 'pointgoal' in self.output:
            state['pointgoal'] = sensor_state[:2]

        if 'scan' in self.output:
            assert 'scan_link' in self.robots[0].parts, "Requested scan but no scan_link"
            pose_camera = self.robots[0].parts['scan_link'].get_pose()
            n_rays_per_horizontal = 128  # Number of rays along one horizontal scan/slice

            n_vertical_beams = 9
            angle = np.arange(0, 2 * np.pi, 2 * np.pi / float(n_rays_per_horizontal))
            elev_bottom_angle = -30. * np.pi / 180.
            elev_top_angle = 10. * np.pi / 180.
            elev_angle = np.arange(elev_bottom_angle, elev_top_angle,get_state
                                   (elev_top_angle - elev_bottom_angle) / float(n_vertical_beams))
            orig_offset = np.vstack([
                np.vstack([np.cos(angle),
                           np.sin(angle),
                           np.repeat(np.tan(elev_ang), angle.shape)]).T for elev_ang in elev_angle
            ])
            transform_matrix = quat2mat(
                [pose_camera[-1], pose_camera[3], pose_camera[4], pose_camera[5]])
            offset = orig_offset.dot(np.linalg.inv(transform_matrix))
            pose_camera = pose_camera[None, :3].repeat(n_rays_per_horizontal * n_vertical_beams,
                                                       axis=0)

            results = p.rayTestBatch(pose_camera, pose_camera + offset * 30)
            hit = np.array([item[0] for item in results])
            dist = np.array([item[2] for item in results])
            dist[dist >= 1 - 1e-5] = np.nan
            dist[dist < 0.1 / 30] = np.nan

            dist[hit == self.robots[0].robot_ids[0]] = np.nan
            dist[hit == -1] = np.nan
            dist *= 30

            xyz = dist[:, np.newaxis] * orig_offset
            xyz = xyz[np.equal(np.isnan(xyz), False)]  # Remove nans
            # print(xyz.shape)
            xyz = xyz.reshape(xyz.shape[0] // 3, -1)
            state['scan'] = xyz

        return state

    def run_simulation(self):
        collision_links = []
        for _ in range(self.simulator_loop):
            self.simulator_step()
            collision_links += list(p.getContactPoints(bodyA=self.robots[0].robot_ids[0]))
        return collision_links

    def get_position_of_interest(self):
        if self.config['task'] == 'pointgoal':
            return self.robots[0].get_position()
        elif self.config['task'] == 'reaching':
            return self.robots[0].get_end_effector_position()

    def get_potential(self):
        return l2_distance(self.target_pos, self.get_position_of_interest())

    def get_reward(self, collision_links):
        reward = self.slack_reward  # |slack_reward| = 0.01 per step

        new_normalized_potential = self.get_potential() / self.initial_potential

        potential_reward = self.normalized_potential - new_normalized_potential
        reward += potential_reward * self.potential_reward_weight  # |potential_reward| ~= 0.1 per step
        self.normalized_potential = new_normalized_potential

        # electricity_reward = np.abs(self.robots[0].joint_speeds * self.robots[0].joint_torque).mean().item()
        electricity_reward = 0.0
        reward += electricity_reward * self.electricity_reward_weight  # |electricity_reward| ~= 0.05 per step

        # stall_torque_reward = np.square(self.robots[0].joint_torque).mean()
        stall_torque_reward = 0.0
        reward += stall_torque_reward * self.stall_torque_reward_weight  # |stall_torque_reward| ~= 0.05 per step

        collision_link_ids = set([elem[3] for elem in collision_links])
        collision_reward = float(len(collision_link_ids & self.collision_links) != 0)
        reward += collision_reward * self.collision_reward_weight  # |collision_reward| ~= 1.0 per step if collision

        # goal reached
        if l2_distance(self.target_pos, self.get_position_of_interest()) < self.dist_tol:
            reward += self.success_reward  # |success_reward| = 10.0 per step

        return reward

    def get_termination(self):
        self.current_step += 1
        done, info = False, {}

        # goal reached
        if l2_distance(self.target_pos, self.get_position_of_interest()) < self.dist_tol:
            # print('goal')
            done = True
            info['success'] = True
        # robot flips over
        elif self.robots[0].get_position()[2] > 0.1:
            print('death')
            done = True
            info['success'] = False
        # time out
        elif self.current_step >= self.max_step:
            # print('timeout')
            done = True
            info['success'] = False

        if done:
            info['episode_length'] = self.current_step

        return done, info

    def step(self, action):
        self.robots[0].apply_action(action)
        collision_links = self.run_simulation()
        state = self.get_state(collision_links)
        reward = self.get_reward(collision_links)
        done, info = self.get_termination()

        if done and self.automatic_reset:
            info['last_observation'] = state
            state = self.reset()
        return state, reward, done, info

    def reset_initial_and_target_pos(self):
        self.robots[0].set_position(pos=self.initial_pos)
        self.robots[0].set_orientation(orn=quatToXYZW(euler2quat(*self.initial_orn), 'wxyz'))

    def reset(self):
        self.robots[0].robot_specific_reset()
        self.reset_initial_and_target_pos()
        self.initial_potential = self.get_potential()
        self.normalized_potential = 1.0
        self.current_step = 0

        # set position for visual objects
        if self.visual_object_at_initial_target_pos:
            self.initial_pos_vis_obj.set_position(self.initial_pos)
            self.target_pos_vis_obj.set_position(self.target_pos)

        state = self.get_state()
        return state


class NavigateRandomEnv(NavigateEnv):
    def __init__(
            self,
            config_file,
            mode='headless',
            action_timestep=1 / 10.0,
            physics_timestep=1 / 240.0,
            automatic_reset=False,
            random_height=False,
            device_idx=0,
    ):
        super(NavigateRandomEnv, self).__init__(config_file,
                                                mode=mode,
                                                action_timestep=action_timestep,
                                                physics_timestep=physics_timestep,
                                                automatic_reset=automatic_reset,
                                                device_idx=device_idx)
        self.random_height = random_height

    def reset_initial_and_target_pos(self):
        collision_links = [-1]
        while -1 in collision_links:  # if collision happens, reinitialize
            floor, pos = self.scene.get_random_point()
            self.robots[0].set_position(pos=[pos[0], pos[1], pos[2] + 0.1])
            self.robots[0].set_orientation(
                orn=quatToXYZW(euler2quat(0, 0, np.random.uniform(0, np.pi * 2)), 'wxyz'))
            collision_links = []
            for _ in range(self.simulator_loop):
                self.simulator_step()
                collision_links += [
                    item[3] for item in p.getContactPoints(bodyA=self.robots[0].robot_ids[0])
                ]
            collision_links = np.unique(collision_links)
            self.initial_pos = pos
        dist = 0.0
        while dist < 1.0:  # if initial and target positions are < 1 meter away from each other, reinitialize
            _, self.target_pos = self.scene.get_random_point_floor(floor, self.random_height)
            dist = l2_distance(self.initial_pos, self.target_pos)


class InteractiveNavigateEnv(NavigateEnv):
    def __init__(self,
                 config_file,
                 mode='headless',
                 action_timestep=1 / 10.0,
                 physics_timestep=1 / 240.0,
                 device_idx=0,
                 automatic_reset=False):
        super(InteractiveNavigateEnv, self).__init__(config_file,
                                                     mode=mode,
                                                     action_timestep=action_timestep,
                                                     physics_timestep=physics_timestep,
                                                     automatic_reset=automatic_reset,
                                                     device_idx=device_idx)
        self.door = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'realdoor.urdf'),
                                   scale=1.35)
        self.simulator.import_interactive_object(self.door)
        # TODO: door pos
        self.door.set_position_rotation([100, 100, -0.02], quatToXYZW(euler2quat(0, 0, np.pi / 2.0), 'wxyz'))
        self.door_angle = self.config.get('door_angle', 90)
        self.door_angle = -(self.door_angle / 180.0) * np.pi
        self.door_handle_link_id = 2
        self.door_axis_link_id = 1
        self.jr_end_effector_link_id = 34

        # TODO: wall
        self.wall1 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
                                    scale=1)
        self.simulator.import_interactive_object(self.wall1)
        self.wall1.set_position_rotation([0, -3, 1], [0, 0, 0, 1])

        self.wall2 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
                                    scale=1)
        self.simulator.import_interactive_object(self.wall2)
        self.wall2.set_position_rotation([0, 3, 1], [0, 0, 0, 1])

        self.wall3 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
                                    scale=1)
        self.simulator.import_interactive_object(self.wall3)
        self.wall3.set_position_rotation([-3, 0, 1], [0, 0, np.sqrt(0.5), np.sqrt(0.5)])

        self.wall4 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
                                    scale=1)
        self.simulator.import_interactive_object(self.wall4)
        self.wall4.set_position_rotation([3, 0, 1], [0, 0, np.sqrt(0.5), np.sqrt(0.5)])
        #
        # self.wall5 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
        #                             scale=1)
        # self.simulator.import_interactive_object(self.wall5)
        # self.wall5.set_position_rotation([0, -7.8, 1], [0, 0, np.sqrt(0.5), np.sqrt(0.5)])
        #
        # self.wall6 = InteractiveObj(os.path.join(gibson2.assets_path, 'models', 'scene_components', 'walls.urdf'),
        #                             scale=1)
        # self.simulator.import_interactive_object(self.wall6)
        # self.wall6.set_position_rotation([0, 7.8, 1], [0, 0, np.sqrt(0.5), np.sqrt(0.5)])

        # dense reward
        self.stage = 0
        self.prev_stage = self.stage
        self.stage_get_to_door_handle = 0
        self.stage_open_door = 1
        self.stage_get_to_target_pos = 2

        # attaching JR's arm to the door handle
        self.cid = None

        # visualize subgoal
        self.subgoal_base = VisualObject(visual_shape=p.GEOM_BOX, rgba_color=[0, 1, 0, 0.5], half_extents=[0.4] * 3)
        self.subgoal_base.load()
        self.subgoal_end_effector = VisualObject(rgba_color=[1, 1, 0, 0.5], radius=0.2)
        self.subgoal_end_effector.load()

    def set_subgoal(self, ideal_next_state):
        obs_avg = (self.observation_normalizer['sensor'][1] + self.observation_normalizer['sensor'][0]) / 2.0
        obs_mag = (self.observation_normalizer['sensor'][1] - self.observation_normalizer['sensor'][0]) / 2.0
        ideal_next_state = (ideal_next_state * obs_mag) + obs_avg

        base_pos = np.zeros(3)
        base_pos[:2] = ideal_next_state[:2]
        base_pos[2] = 0.4

        yaw = ideal_next_state[5]
        new_orn = quatToXYZW(euler2quat(0, 0, yaw), 'wxyz')

        end_effector_pos = ideal_next_state[2:5]
        end_effector_pos = rotate_vector_3d(end_effector_pos, 0, 0, -yaw)

        self.subgoal_base.set_position(base_pos, new_orn=new_orn)
        self.subgoal_end_effector.set_position(base_pos + end_effector_pos - 0.4)

    def reset_interactive_objects(self):
        p.resetJointState(self.door.body_id, self.door_axis_link_id, targetValue=0.0, targetVelocity=0.0)
        if self.cid is not None:
            p.removeConstraint(self.cid)
            self.cid = None

    def reset_initial_and_target_pos(self):
        collision_links = [-1]
        while -1 in collision_links:  # if collision happens restart
            # pos = [np.random.uniform(1, 2), np.random.uniform(-0.5, 0.5), 0]
            pos = [0.0, 0.0, 0]
            self.robots[0].set_position(pos=[pos[0], pos[1], pos[2] + 0.1])
            self.robots[0].set_orientation(orn=quatToXYZW(euler2quat(0, 0, np.random.uniform(0, np.pi * 2)), 'wxyz'))
            # self.robots[0].set_orientation(orn=quatToXYZW(euler2quat(0, 0, np.pi), 'wxyz'))
            collision_links = []
            for _ in range(self.simulator_loop):
                self.simulator_step()
                collision_links += [
                    item[3] for item in p.getContactPoints(bodyA=self.robots[0].robot_ids[0])
                ]
            collision_links = np.unique(collision_links)
            self.initial_pos = pos

        # wait for the base to fall down to the ground and for the arm to move to its initial position
        for _ in range(int(0.5 / self.physics_timestep)):
            self.simulator_step()

        # self.target_pos = [np.random.uniform(-2, -1), np.random.uniform(-0.5, 0.5), 0]
        # TODO: target pos
        self.target_pos = [-100, -100, 0]
        # self.target_pos = np.array([-1.0, 0.0, 0])

    def reset(self):
        self.reset_interactive_objects()
        self.stage = 0
        self.prev_stage = self.stage
        return super(InteractiveNavigateEnv, self).reset()

    def wrap_to_pi(self, states, indices):
        states[indices] = states[indices] - np.pi * 2 * np.floor((states[indices] + np.pi) / (np.pi * 2))
        return states

    def get_state(self, collision_links=[]):
        state = super(InteractiveNavigateEnv, self).get_state()
        # self.state_stats['sensor'].append(state['sensor'])
        # self.state_stats['auxiliary_sensor'].append(state['auxiliary_sensor'])
        if self.normalize_observation:
            for key in state:
                obs_min = self.observation_normalizer[key][0]
                obs_max = self.observation_normalizer[key][1]
                obs_avg = (self.observation_normalizer[key][1] + self.observation_normalizer[key][0]) / 2.0
                obs_mag = (self.observation_normalizer[key][1] - self.observation_normalizer[key][0]) / 2.0
                state[key] = (np.clip(state[key], obs_min, obs_max) - obs_avg) / obs_mag  # normalize to [-1, 1]
        # self.state_stats['rgb'].append(state['rgb'])
        # self.state_stats['depth'].append(state['depth'])
        return state

    def get_additional_states(self):
        robot_position = self.robots[0].get_position()[:2]  # z is not controllable by the agent
        end_effector_pos = self.robots[0].get_end_effector_position() - self.robots[0].get_position()
        end_effector_pos = rotate_vector_3d(end_effector_pos, *self.robots[0].get_rpy())
        _, _, yaw = self.robots[0].get_rpy()
        door_angle = p.getJointState(self.door.body_id, self.door_axis_link_id)[0]
        additional_states = np.concatenate([robot_position, end_effector_pos, [yaw, door_angle]])
        additional_states = self.wrap_to_pi(additional_states, np.array([5, 6]))
        assert len(additional_states) == self.additional_states_dim, 'additional states dimension mismatch'
        return additional_states

    def get_auxiliary_sensor(self):
        auxiliary_sensor = np.zeros(self.auxiliary_sensor_dim)
        robot_state = self.robots[0].calc_state()
        assert self.auxiliary_sensor_dim == 42
        assert robot_state.shape[0] == 46

        auxiliary_sensor[:6] = robot_state[:6]        # z, vx, vy, vz, roll, pitch
        auxiliary_sensor[6:12] = robot_state[7:13]    # wheel 1, 2
        auxiliary_sensor[12:27] = robot_state[19:34]  # arm joint 1, 2, 3, 4, 5
        auxiliary_sensor[27:30] = robot_state[43:46]  # v_roll, v_pitch, v_yaw

        r, p, yaw = self.robots[0].get_rpy()
        cos_yaw, sin_yaw = np.cos(yaw), np.sin(yaw)
        has_door_handle_in_hand = 1.0 if self.stage == self.stage_open_door else -1.0
        door_pos = np.array([0, 0, -0.02])
        target_pos = self.target_pos
        robot_pos = self.robots[0].get_position()
        door_pos_local = rotate_vector_3d(door_pos - robot_pos, r, p, yaw)
        target_pos_local = rotate_vector_3d(target_pos - robot_pos, r, p, yaw)

        auxiliary_sensor[30:33] = np.array([cos_yaw, sin_yaw, has_door_handle_in_hand])
        auxiliary_sensor[33:36] = target_pos
        auxiliary_sensor[36:39] = door_pos_local
        auxiliary_sensor[39:42] = target_pos_local

        auxiliary_sensor = self.wrap_to_pi(auxiliary_sensor, np.arange(12, 27, 3))
        return auxiliary_sensor

    def step(self, action):
        dist = np.linalg.norm(
            np.array(p.getLinkState(self.door.body_id, self.door_handle_link_id)[0]) -
            np.array(p.getLinkState(self.robots[0].robot_ids[0], self.jr_end_effector_link_id)[0])
        )
        # print(dist)
        self.prev_stage = self.stage
        if self.stage == self.stage_get_to_door_handle and dist < 0.2:
            assert self.cid is None
            self.cid = p.createConstraint(self.robots[0].robot_ids[0], self.jr_end_effector_link_id,
                                          self.door.body_id, self.door_handle_link_id,
                                          p.JOINT_POINT2POINT, [0, 0, 0],
                                          [0, 0.0, 0], [0, 0, 0])
            p.changeConstraint(self.cid, maxForce=500)
            self.stage = self.stage_open_door
            print("stage open_door")

        if self.stage == self.stage_open_door and p.getJointState(self.door.body_id, 1)[0] < self.door_angle:  # door open > 45/60/90 degree
            assert self.cid is not None
            p.removeConstraint(self.cid)
            self.cid = None
            self.stage = self.stage_get_to_target_pos
            print("stage get to target pos")
        # print("door info", p.getJointInfo(self.door.body_id, 1))
        # print("door angle", p.getJointState(self.door.body_id, 1)[0])
        return super(InteractiveNavigateEnv, self).step(action)

    def get_potential(self):
        door_angle = p.getJointState(self.door.body_id, self.door_axis_link_id)[0]
        door_handle_pos = p.getLinkState(self.door.body_id, self.door_handle_link_id)[0]
        if self.stage == self.stage_get_to_door_handle:
            potential = l2_distance(door_handle_pos, self.robots[0].get_end_effector_position())
        elif self.stage == self.stage_open_door:
            potential = np.abs(door_angle + np.pi)
        elif self.stage == self.stage_get_to_target_pos:
            potential = l2_distance(self.target_pos, self.robots[0].get_position())
        # print("get_potential (stage %d): %f" % (self.stage, potential))
        return potential

    def get_reward(self, collision_links):
        reward = 0.0
        if self.stage != self.prev_stage:
            # advance to the next stage
            self.initial_potential = self.get_potential()
            self.normalized_potential = 1.0
            reward += self.success_reward / 2.0
        else:
            new_normalized_potential = self.get_potential() / self.initial_potential
            potential_reward = self.normalized_potential - new_normalized_potential
            reward += potential_reward * self.potential_reward_weight  # |potential_reward| ~= 0.1 per step
            # self.reward_stats.append(np.abs(potential_reward * self.potential_reward_weight))
            self.normalized_potential = new_normalized_potential

        electricity_reward = np.abs(self.robots[0].joint_speeds * self.robots[0].joint_torque).mean().item()
        # electricity_reward = 0.0
        reward += np.clip(electricity_reward * self.electricity_reward_weight, -0.005, 0)  # |electricity_reward| ~= 0.005 per step
        # self.reward_stats.append(np.abs(electricity_reward * self.electricity_reward_weight))

        stall_torque_reward = np.square(self.robots[0].joint_torque).mean()
        # stall_torque_reward = 0.0
        reward += np.clip(stall_torque_reward * self.stall_torque_reward_weight, -0.005, 0)  # |stall_torque_reward| ~= 0.005 per step

        collision_link_ids = set([elem[3] for elem in collision_links
                                  if not (elem[2] == self.door.body_id and elem[4] == self.door_handle_link_id)])
        collision_reward = float(len(collision_link_ids & self.collision_links) != 0)
        reward += collision_reward * self.collision_reward_weight  # |collision_reward| ~= 1.0 per step if collision
        # self.reward_stats.append(np.abs(collision_reward * self.collision_reward_weight))

        # goal reached
        if l2_distance(self.target_pos, self.get_position_of_interest()) < self.dist_tol:
            reward += self.success_reward  # |success_reward| = 10.0

        # death penalty
        if self.robots[0].get_position()[2] > 0.1:
            reward -= self.success_reward

        # print("get_reward (stage %d): %f" % (self.stage, reward))
        return reward


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--robot',
                        '-r',
                        choices=['turtlebot', 'jr'],
                        required=True,
                        help='which robot [turtlebot|jr]')
    parser.add_argument(
        '--config',
        '-c',
        help='which config file to use [default: use yaml files in examples/configs]')
    parser.add_argument('--mode',
                        '-m',
                        choices=['headless', 'gui'],
                        default='headless',
                        help='which mode for simulation (default: headless)')
    parser.add_argument('--env_type',
                        choices=['deterministic', 'random', 'interactive'],
                        default='deterministic',
                        help='which environment type (deterministic | random | interactive')
    args = parser.parse_args()

    if args.robot == 'turtlebot':
        config_filename = os.path.join(os.path.dirname(gibson2.__file__),
                                       '../examples/configs/turtlebot_p2p_nav_discrete.yaml') \
            if args.config is None else args.config
    elif args.robot == 'jr':
        config_filename = os.path.join(os.path.dirname(gibson2.__file__),
                                       '../examples/configs/jr2_reaching.yaml') \
            if args.config is None else args.config
    if args.env_type == 'deterministic':
        nav_env = NavigateEnv(config_file=config_filename,
                              mode=args.mode,
                              action_timestep=1.0 / 10.0,
                              physics_timestep=1 / 40.0)
    elif args.env_type == 'random':
        nav_env = NavigateRandomEnv(config_file=config_filename,
                                    mode=args.mode,
                                    action_timestep=1.0 / 10.0,
                                    physics_timestep=1 / 40.0)
    else:
        nav_env = InteractiveNavigateEnv(config_file=config_filename,
                                         mode=args.mode,
                                         action_timestep=1.0 / 10.0,
                                         physics_timestep=1 / 40.0)

    # debug_params = [p.addUserDebugParameter('link%d' % i, -1.0, 1.0, 0) for i in range(1, 6)]
    # nav_env.reset()
    # for i in range(1000000):  # 500 steps, 50s world time
    #     debug_param_values = [p.readUserDebugParameter(debug_param) for debug_param in debug_params]
    #     action = np.zeros(nav_env.action_space.shape)
    #     action[2:] = np.array(debug_param_values)
    #     print(action)
    #     nav_env.step(action)
    # assert False

    for episode in range(20):
        print('Episode: {}'.format(episode))
        nav_env.reset()
        for i in range(500):  # 500 steps, 50s world time
            action = nav_env.action_space.sample()
            # action[0:2] = 0.05
            state, reward, done, _ = nav_env.step(action)
            # print(reward)
            # print(nav_env.stage)
            # embed()
            if done:
                print('Episode finished after {} timesteps'.format(i + 1))
                break
        # print('len', len(nav_env.reward_stats))
        # print('mean', np.mean(nav_env.reward_stats))
        # print('median', np.median(nav_env.reward_stats))
        # print('max', np.max(nav_env.reward_stats))
        # print('min', np.min(nav_env.reward_stats))
        # print('std', np.std(nav_env.reward_stats))
        # print('95 percentile', np.percentile(nav_env.reward_stats, 95))
        # print('99 percentile', np.percentile(nav_env.reward_stats, 99))
    embed()

    nav_env.clean()