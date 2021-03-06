import warnings
import heapq
import simpy
import argparse
import tensorflow
from utils.args import parse_args
import simpy
import numpy as np
import random
from simpy.events import AnyOf, AllOf
import sys
from datetime import datetime

import os
# os.path.join()

latest_n = 5  # 取最近五个预测带宽取平均
init_pridict_bandwidth = 10  # 初始化预测带宽的大小
e = 0.5
CAPACITY = 100
SEG_SIZE = 30
TRAINING_TIME = 30
g = 0.01


class Client:
    def __init__(self, aggregation, e, env, idx, clients_num, client_id, group=None, train_data={'x': [], 'y': []},
                 eval_data={'x': [], 'y': []}, model=None):
        self._model = model
        self.aggregation = aggregation
        self.e = e
        self.id = client_id  # integer
        self.idx = int(idx)
        self.clients_num = clients_num
        self.group = group
        self.train_data = train_data
        self.eval_data = eval_data
        self.pridict_bandwidth = []  # 初始化该client到其他所有节点的预测带宽
        for i in range(self.clients_num):
            if i ==self.idx:
                self.pridict_bandwidth.append([1])
            else:
                self.pridict_bandwidth.append([init_pridict_bandwidth])
        self.updates = []
        self.model_para = model.get_params()
        self.train_time = []
        self.transfer_time = []
        self.updates_flat = []
        self.exit_bw = simpy.Container(env, init=CAPACITY, capacity=CAPACITY)
        self.record_time = {0:0}
        # self.training_time = [env.now]
        # self.seg_transfer_time = [0] * clients_num
        self.send_que = simpy.Container(env, init=0, capacity=1000)
        self.sigal = False
        # self.max_seg_transfer_time = 0
        self.round_signal = False
        self.training_time = 0
        self.metrics = {}
        self.local_update = model.get_params()
        for i in range(clients_num):
            a = []
            for j in range(clients_num):
                a.append(-1)
            self.transfer_time.append(a)
        self.args = parse_args()
        self.test_signal =False
        self.model_shape = np.array(self.flat_updates(self.model_para)).shape
        self.m = np.zeros(self.model_shape)
        self.v = np.zeros(self.model_shape)



    def train(self, server, num_epochs=1, batch_size=10, minibatch=None):
        """Trains on self.model using the client's train_data.

        Args:
            num_epochs: Number of epochs to train. Unsupported if minibatch is provided (minibatch has only 1 epoch)
            batch_size: Size of training batches.
            minibatch: fraction of client's data to apply minibatch sgd,
                None to use FedAvg
        Return:
            comp: number of FLOPs executed in training process
            num_samples: number of samples used in training
            update: set of weights
            update_size: number of bytes in update
        """
        # print(65342452435,self.id)
        start_time = datetime.now()
        if minibatch is None:
            data = self.train_data
            comp, update,accumulative_gradient = self.model.train_tau(data, num_epochs, batch_size)
        else:
            frac = min(1.0, minibatch)
            num_data = max(1, int(frac * len(self.train_data["x"])))
            xs, ys = zip(*random.sample(list(zip(self.train_data["x"], self.train_data["y"])), num_data))
            data = {'x': xs, 'y': ys}
            # Minibatch trains for only 1 epoch - multiple local epochs don't make sense!
            num_epochs = 1
            comp, update = self.model.train(data, num_epochs, num_data)

        if self.aggregation !='weight':
            self.local_update = self.flat_updates(update)
            update = accumulative_gradient
        # else:
        num_train_samples = len(data['y'])
        self.update = self.flat_updates(update)  # save flattened model weights
        # self.updates.append((num_train_samples, update))
        server.updates.append((num_train_samples, self.update))
        end_time = datetime.now()
        self.training_time = (end_time - start_time).seconds
        return comp, num_train_samples, update

    def adam(self,gradient,para,my_round,beta_1=0.9,beta_2=0.999,epsilon=10e-8,step_size = 0.001):
        g = gradient
        self.m = beta_1 * self.m + (1 - beta_1) * g
        self.v = beta_2 * self.v + (1 - beta_2) * np.power(g, 2)
        m_hat = self.m / (1 - np.power(beta_1, my_round+1))
        v_hat = self.v / (1 - np.power(beta_2, my_round+1))
        para = para - step_size * m_hat / (np.sqrt(v_hat) + epsilon)
        return para

    def flat_updates(self, model_weights):
        self.shape_list = []
        flat_m = []
        for x in model_weights:
            self.shape_list.append(x.shape)
            flat_m.extend(list(x.ravel()))
        return flat_m

    def update_model(self, replica, segment, server,num,my_round,adam_step_size):
        print('-----update[%s]------' % self.idx)
        weight_list = []
        sum_sample = 0
        for p in range(segment):
            target = self.choose_best_segment(e, replica,num,my_round,p)
            segment_weight = server.updates[self.idx][0] * self.get_segments(server.updates[self.idx][1], p, segment)
            sum_sample += server.updates[self.idx][0]
            for k in range(replica):
                segment_weight += server.updates[target[k]][0] * self.get_segments(server.updates[target[k]][1], p,
                                                                                   segment)
                sum_sample += server.updates[target[k]][0]
            weight_list = np.concatenate((weight_list, segment_weight), axis=None)
        average_weight = segment * np.float64(weight_list) / sum_sample
        if self.aggregation == 'sgd':
            # print(11111,np.array(self.local_update).shape, np.array(average_weight).shape)
            average_weight = self.local_update - 0.003 * average_weight
        elif self.aggregation == 'adam':
            average_weight = self.adam(average_weight, self.local_update, my_round,step_size=adam_step_size)
        self.model_para = self.reconstruct(average_weight)
        # model_gradient = self.reconstruct()
        # average_weight = self.adam(average_weight,self.local_update,my_round)
        # self.model_para = model_para_t-self.updates

    def train_time_simulate(self, env, my_round, client_simulate_list, bandwidth, replica, seg,num):
        if my_round != 0:
            while not self.round_signal:
                yield env.timeout(0.01)
        self.round_signal = False
        print('-------------client [%d] round [%s] begin at %f:------------' % (self.idx, my_round, env.now))
        yield env.timeout(self.training_time)
        self.sigal = True
        idx_list = self.get_idx_list(e, replica, seg,num,my_round)
        print('[Time:', env.now, ']', self.idx, 'pull from', idx_list)
        events = [env.process(self.get_transfer_time(env, client_simulate_list, bandwidth, my_round, i, seg)) for i in
                  idx_list]
        yield AllOf(env, events)
        print('[Time:', env.now, '][Round: %s][Id: %d]' % (my_round, self.idx), 'transfer')
        self.record_time[my_round+1] = env.now
        self.round_signal = True

    def choose_best_segment(self, e, replica,num,my_round,seg):
        target = []
        if self.args.algorithm == 'BACombo':
            if num < self.e:
                # print('random select')
                # # client_num_list = list(range(client_num))
                # # client_num_list.remove(self.idx)
                # # return np.random.choice(client_num_list, size=k, replace=False, p=None)
                client_candidate = list(range(self.clients_num))  # .remove(self.idx)
                client_candidate.remove(self.idx)
                # for i in range(replica):
                np.random.seed(my_round+self.idx+seg)
                target = np.random.choice(client_candidate, size=replica, replace=False)
                # a = np.random.randint(0, self.clients_num)
                # while a == self.idx or a in target:
                #     a = np.random.randint(0, self.clients_num)
                # print('find one for ', i)
                # target.append(a)
            else:
                print('find max bandwidth')
                pridict = []
                for bandwidth in self.pridict_bandwidth:
                    if len(bandwidth) < latest_n:
                        pridict.append(np.mean(bandwidth))
                    else:
                        pridict.append(np.mean(bandwidth[-latest_n:]))
                target = np.argpartition(pridict,-replica)[-replica:]
                np.random.shuffle(target)
                # target = list(map(pridict.index, heapq.nlargest(latest_n, self.pridict_bandwidth)))
        else:
            # print('select target')
            client_candidate = list(range(self.clients_num))
            client_candidate.remove(self.idx)
            np.random.seed(my_round + self.idx +seg)
            target = np.random.choice(client_candidate, size=replica, replace=False)
            # for i in range(replica):
            #     a = np.random.randint(0, self.clients_num)
            #     while a == self.idx or a in target:
            #         a = np.random.randint(0, self.clients_num)
            #     target.append(a)
        return target

    def get_segments(self, flat_m, seg, segment):
        # flat_m = []
        # self.shape_list = []
        # print('the len of weights', len(model_weights))
        # for x in model_weights:
        #     self.shape_list.append(x.shape)
        #     flat_m.extend(list(x.flatten()))
        seg_length = len(flat_m) // segment + 1

        return np.array(flat_m[seg * seg_length:(seg + 1) * seg_length])

    def reconstruct(self, flat_m):
        result = []
        current_pos = 0
        # print('222', self.shape_list)
        for shape in self.shape_list:
            total_number = 1
            for i in shape:
                total_number *= i
            result.append(np.array(flat_m[current_pos:current_pos + total_number]).reshape(shape))
            current_pos += total_number
        return np.array(result)

    def update_bandwidth(self, seg):
        seg_size = sys.getsizeof(self.updates)
        time = self.transfer_time[self.idx]
        # client_idx_list =
        for i in range(self.clients_num):
            if time[i] != -1:
                self.pridict_bandwidth[i].append((seg_size / seg) / time[i])
        # print(6546245,self.idx,time,self.pridict_bandwidth)


    def get_transfer_time(self, env, client_simulate_list, bandwidth, my_round, idx_list, seg):
        events = [env.process(self.pull_seg(env, client_simulate_list[i], i, bandwidth, my_round, seg))
                  for i in idx_list]
        yield AllOf(env, events)
        # print(12132424,self.idx)
        # self.max_seg_transfer_time = np.max(self.seg_transfer_time)

    def get_idx_list(self, e, replica, seg,num,my_round):
        idx_list = []
        for i in range(seg):
            target = self.choose_best_segment(e, replica,num,my_round,i)
            idx_list.append(target)
        return idx_list
        # client_num_list = list(range(client_num))
        # client_num_list.remove(self.idx)
        # return np.random.choice(client_num_list, size=k, replace=False, p=None)

    def pull_seg(self, env, client_simulate, idx, bandwidth, my_round, seg):
        while not client_simulate.sigal:
            yield env.timeout(0.01)
        yield client_simulate.send_que.put(1)
        exit_bw = client_simulate.exit_bw.level
        bottleneck_num = client_simulate.send_que.level
        # print(324413,idx,self.idx,bandwidth)
        link_bw = bandwidth[idx][self.idx]
        start_time = env.now
        yield env.process(self.que_monitor(env, client_simulate, exit_bw, link_bw, bottleneck_num, seg))
        end_time = env.now
        self.transfer_time[self.idx][client_simulate.idx] = end_time - start_time
        yield client_simulate.send_que.get(1)
        # print('[Time:', env.now,
        #       '][Round: %s][Id: %d]-----pulling-----[Id：%d]' % (my_round, self.idx, client_simulate.idx))

    def que_monitor(self, env, client_simulate, exit_bw, link_bw, bottleneck_num, seg):
        seg_size = sys.getsizeof(self.updates)
        self.updates = []
        residual_seg_size = seg_size / seg
        final_bw = np.min([exit_bw / bottleneck_num, link_bw])
        last_que = bottleneck_num
        # print(41234134,link_bw,self.idx)
        while residual_seg_size >= 0:
            yield env.timeout(g)
            residual_seg_size -= g * final_bw
            current_que = client_simulate.send_que.level
            if current_que != last_que:
                last_que = current_que
                final_bw = np.min([exit_bw / current_que, link_bw])
            else:
                last_que = current_que

    def test(self,my_round,set_to_use='test'):
        """Tests self.model on self.test_data.

        Args:
            set_to_use. Set to test on. Should be in ['train', 'test'].
        Return:
            dict of metrics returned by the model.
        """
        # while not self.sigal:
            # continue
            # yield env.timeout(0.01)
        assert set_to_use in ['train', 'test']
        if set_to_use == 'train':
            data = self.train_data
        elif set_to_use == 'test':
            data = self.eval_data
        self.model.set_params(self.model_para)
        metrics = self.model.test(data)
        if metrics['accuracy']<=0.01:
            print(11111)
        # if set_to_use =='test':
        metrics['time'] = self.record_time[my_round]

        # metrics['']

        # try:
        #     metrics['time'] = self.record_time[my_round]
        # except:
        #     print(7777,self.id,my_round,self.record_time)
        return metrics

    @property
    def num_test_samples(self):
        """Number of test samples for this client.

        Return:
            int: Number of test samples for this client
        """
        if self.eval_data is None:
            return 0
        return len(self.eval_data['y'])

    @property
    def num_train_samples(self):
        """Number of train samples for this client.

        Return:
            int: Number of train samples for this client
        """
        if self.train_data is None:
            return 0
        return len(self.train_data['y'])

    @property
    def num_samples(self):
        """Number samples for this client.

        Return:
            int: Number of samples for this client
        """
        train_size = 0
        if self.train_data is not None:
            train_size = len(self.train_data['y'])

        test_size = 0
        if self.eval_data is not None:
            test_size = len(self.eval_data['y'])
        return train_size + test_size

    @property
    def model(self):
        """Returns this client reference to model being trained"""
        return self._model

    @model.setter
    def model(self, model):
        warnings.warn('The current implementation shares the model among all clients.'
                      'Setting it on one client will effectively modify all clients.')
        self._model = model
