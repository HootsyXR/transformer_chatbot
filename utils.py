import re
import os
import json
from collections import namedtuple

import torch
import numpy as np
import scipy
from torch.utils.checkpoint import checkpoint

def pad_sequence(sequences, batch_first=False, padding_value=0):
    # assuming trailing dimensions and type of all the Tensors
    # in sequences are same and fetching those from sequences[0]
    max_size = sequences[0].size()
    trailing_dims = max_size[1:]
    max_len = max([s.size(0) for s in sequences])
    if batch_first:
        out_dims = (len(sequences), max_len) + trailing_dims
    else:
        out_dims = (max_len, len(sequences)) + trailing_dims

    out_tensor = sequences[0].data.new(*out_dims).fill_(padding_value)
    for i, tensor in enumerate(sequences):
        length = tensor.size(0)
        # use index notation to prevent duplicate references to the tensor
        if batch_first:
            out_tensor[i, :length, ...] = tensor
        else:
            out_tensor[:length, i, ...] = tensor

    return out_tensor


def checkpoint_sequential(functions, segments, *inputs):
    def run_function(start, end, functions):
        def forward(*inputs):
            for j in range(start, end + 1):
                inputs = functions[j](*inputs)
            return inputs
        return forward

    if isinstance(functions, torch.nn.Sequential):
        functions = list(functions.children())

    segment_size = len(functions) // segments
    # the last chunk has to be non-volatile
    end = -1
    for start in range(0, segment_size * (segments - 1), segment_size):
        end = start + segment_size - 1
        inputs = checkpoint(run_function(start, end, functions), *inputs)
        if not isinstance(inputs, tuple):
            inputs = (inputs,)
    return run_function(end + 1, len(functions) - 1, functions)(*inputs)


def openai_transformer_config():
    class dotdict(dict):
        __getattr__ = dict.get
        __setattr__ = dict.__setitem__
        __delattr__ = dict.__delitem__

    cfg = dotdict({'n_layers': 12, 'n_embeddings': 40477, 'n_pos_embeddings': 512, 
                   'embeddings_size': 768, 'n_heads': 12, 'dropout': 0.1,
                   'embed_dropout': 0.1, 'attn_dropout': 0.1, 'ff_dropout': 0.1})

    return cfg


def load_openai_weights(model, directory, n_special_tokens=0):
    # TODO: add check of shapes

    parameters_names_path = os.path.join(directory, 'parameters_names.json')
    parameters_shapes_path = os.path.join(directory, 'parameters_shapes.json')
    parameters_weights_paths = [os.path.join(directory, 'params_{}.npy'.format(n)) for n in range(10)]

    with open(parameters_names_path, 'r') as parameters_names_file:
        parameters_names = json.load(parameters_names_file)

    with open(parameters_shapes_path, 'r') as parameters_shapes_file:
        parameters_shapes = json.load(parameters_shapes_file)

    parameters_weights = [np.load(path) for path in parameters_weights_paths]
    parameters_offsets = np.cumsum([np.prod(shape) for shape in parameters_shapes])
    parameters_weights = np.split(np.concatenate(parameters_weights, 0), parameters_offsets)[:-1]
    parameters_weights = [p.reshape(s) for p, s in zip(parameters_weights, parameters_shapes)]

    parameters_weights[1] = parameters_weights[1][1:] # skip 0 - <unk> 


    if model.pos_embeddings.num_embeddings - 1 > parameters_weights[0].shape[0]:
        xx = np.linspace(0, parameters_weights[0].shape[0], model.pos_embeddings.num_embeddings - 1)
        new_kernel = scipy.interpolate.RectBivariateSpline(np.arange(parameters_weights[0].shape[0]),
                                                           np.arange(parameters_weights[0].shape[1]), 
                                                           parameters_weights[0])
        parameters_weights[0] = new_kernel(xx, np.arange(parameters_weights[0].shape[1]))

    parameters_weights[0] = parameters_weights[0][:model.pos_embeddings.num_embeddings - 1]
    parameters_weights[1] = parameters_weights[1][:model.embeddings.num_embeddings - n_special_tokens]

    model.pos_embeddings.weight.data[1:] = torch.from_numpy(parameters_weights[0])
    model.embeddings.weight.data[n_special_tokens:] = torch.from_numpy(parameters_weights[1])


    parameters_weights = parameters_weights[2:]

    for name, weights in zip(parameters_names, parameters_weights):
        name = name[6:]  # skip "model/"
        assert name[-2:] == ':0'
        name = name[:-2]
        name = name.split('/')

        pointer = model
        for m_name in name:
            if re.fullmatch(r'[A-Za-z]+\d+', m_name):
                l = re.split(r'(\d+)', m_name)
            else:
                l = [m_name]

            pointer = getattr(pointer, l[0])

            if len(l) >= 2:
                num = int(l[1])
                pointer = pointer[num]

        if len(weights.shape) == 3: # conv1d to linear
            weights = weights[0].transpose((1, 0))

        pointer.data[...] = torch.from_numpy(weights)
