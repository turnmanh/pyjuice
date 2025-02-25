from __future__ import annotations

import torch
import networkx as nx
from typing import Type
from pyjuice.graph import *
from pyjuice.layer import *
from pyjuice.model import TensorCircuit
from pyjuice.nodes import multiply, summate, inputs, set_block_size, CircuitNodes
from pyjuice.nodes.distributions import Distribution, Categorical
from pyjuice.utils.util import max_cdf_power_of_2
from .compilation import BayesianTreeToHiddenRegionGraph


def HMM(seq_length: int, num_latents: int, num_emits: int, homogeneous: bool = True):
    """
    Constructs Hidden Markov Models.

    :param seq_length: sequence length
    :type seq_length: int

    :param num_latents: size of the latent space
    :type num_latents: int

    :param num_emits: size of the emission space
    :type num_emits: int

    :param homogeneous: whether to define a homogeneous (or inhomogeneous) HMM
    :type homogeneous: bool
    """
    
    block_size = min(max_cdf_power_of_2(num_latents), 1024)
    num_node_blocks = num_latents // block_size
    
    with set_block_size(block_size = block_size):

        ns_input = inputs(
            seq_length - 1, num_node_blocks = num_node_blocks,
            dist = Categorical(num_cats = num_emits)
        )
        
        ns_sum = None
        curr_zs = ns_input
        for var in range(seq_length - 2, -1, -1):
            curr_xs = ns_input.duplicate(var, tie_params = homogeneous)
            
            if ns_sum is None:
                ns = summate(curr_zs, num_node_blocks = num_node_blocks)
                ns_sum = ns
            else:
                ns = ns_sum.duplicate(curr_zs, tie_params = homogeneous)

            curr_zs = multiply(curr_xs, ns)
            
        ns = summate(curr_zs, num_node_blocks = 1, block_size = 1)
    
    return ns
