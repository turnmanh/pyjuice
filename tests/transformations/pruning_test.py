import torch
import pyjuice as juice
from pyjuice.nodes import multiply, summate, inputs
from pyjuice.transformations import prune_by_score
import pyjuice.nodes.distributions as dists

import pytest


def test_pruning():
    num_node_blocks = 2

    for block_size in [1, 2, 4, 8]:
        
        with juice.set_block_size(block_size):

            i0 = inputs(0, num_node_blocks, dists.Categorical(num_cats = 5))
            i1 = inputs(1, num_node_blocks, dists.Categorical(num_cats = 5))
            i2 = inputs(2, num_node_blocks, dists.Categorical(num_cats = 5))
            i3 = inputs(3, num_node_blocks, dists.Categorical(num_cats = 5))

            m1 = multiply(i0, i1)
            n1 = summate(m1, num_node_blocks = num_node_blocks)

            m2 = multiply(i2, i3)
            n2 = summate(m2, num_node_blocks = num_node_blocks)

            m = multiply(n1, n2)
            n = summate(m, num_node_blocks = 1)

            n.init_parameters(perturbation = 2.0)

            n1._scores = torch.Tensor([0.3, 0.2, 0.6, 0.8])
            n2._scores = torch.Tensor([0.3, 0.9, 0.1, 0.8])
            n._scores = torch.Tensor([0.6, 0.6])

            new_n = prune_by_score(n, score_threshold = 0.5)

            assert new_n.edge_ids.size(1) == 2

            new_n1 = new_n.chs[0].chs[0]
            assert new_n1.edge_ids.size(1) == 3
            assert torch.all(new_n1.edge_ids == torch.tensor([[0,1,1],[0,0,1]]))
            assert torch.all(torch.abs(new_n1._params[1:] - n1._params[[2,3]]) < 1e-4)
            assert torch.all(torch.abs(new_n1._params[0].sum(dim = 1) - 1.0) < 1e-4)

            new_n2 = new_n.chs[0].chs[1]
            assert new_n2.edge_ids.size(1) == 2
            assert torch.all(new_n2.edge_ids == torch.tensor([[0,1],[1,1]]))
            assert torch.all(torch.abs(new_n2._params.sum(dim = 2) - 1.0) < 1e-4)


def test_pruning_with_param_tying():
    num_node_blocks = 2

    for block_size in [1, 2, 4, 8]:
        
        with juice.set_block_size(block_size):

            i0 = inputs(0, num_node_blocks, dists.Categorical(num_cats = 5))
            i1 = inputs(1, num_node_blocks, dists.Categorical(num_cats = 5))
            i2 = inputs(2, num_node_blocks, dists.Categorical(num_cats = 5))
            i3 = inputs(3, num_node_blocks, dists.Categorical(num_cats = 5))

            m1 = multiply(i0, i1)
            n1 = summate(m1, num_node_blocks = num_node_blocks)

            m2 = multiply(i2, i3)
            n2 = n1.duplicate(m2, tie_params = True)

            m = multiply(n1, n2)
            n = summate(m, num_node_blocks = 1)

            n.init_parameters(perturbation = 2.0)

            n1._scores = torch.Tensor([0.3, 0.2, 0.6, 0.8])
            n._scores = torch.Tensor([0.6, 0.6])

            new_n = prune_by_score(n, score_threshold = 0.5)

            new_n1 = new_n.chs[0].chs[0]
            assert new_n1.edge_ids.size(1) == 3
            assert torch.all(new_n1.edge_ids == torch.tensor([[0,1,1],[0,0,1]]))
            assert torch.all(torch.abs(new_n1._params[1:] - n1._params[[2,3]]) < 1e-4)
            assert torch.all(torch.abs(new_n1._params[0].sum(dim = 1) - 1.0) < 1e-4)

            new_n2 = new_n.chs[0].chs[1]
            assert new_n2.edge_ids.size(1) == 3
            assert torch.all(new_n2.edge_ids == torch.tensor([[0,1,1],[0,0,1]]))
            assert torch.all(torch.abs(new_n2._source_node._params[1:] - n1._params[[2,3]]) < 1e-4)
            assert torch.all(torch.abs(new_n2._source_node._params[0].sum(dim = 1) - 1.0) < 1e-4)


def test_pruning_by_flow():
    num_nodes = 2

    i0 = inputs(0, num_nodes, dists.Categorical(num_cats = 5))
    i1 = inputs(1, num_nodes, dists.Categorical(num_cats = 5))
    i2 = inputs(2, num_nodes, dists.Categorical(num_cats = 5))
    i3 = inputs(3, num_nodes, dists.Categorical(num_cats = 5))

    m1 = multiply(i0, i1)
    n1 = summate(m1, num_nodes = num_nodes)

    m2 = multiply(i2, i3)
    n2 = summate(m2, num_nodes = num_nodes)

    m = multiply(n1, n2)
    n = summate(m, num_nodes = 1)

    n.init_parameters(perturbation = 2.0)

    device = torch.device("cuda:0")

    pc = juice.TensorCircuit(n)
    pc.to(device)

    data = torch.randint(0, 5, [16, 4]).to(device)

    # Run forward and backward manually to get the edge flows
    # If there are more samples, just do this iteratively for 
    # all batches. The flows will be accumulated automatically.
    lls = pc(data)
    pc.backward(data.permute(1, 0))

    pc.update_parameters() # Map the flows back to their corresponding nodes
    pc.update_param_flows()

    new_n = prune_by_score(n, key = "_param_flows", score_threshold = 0.5) # Use `n._flows` for pruning


if __name__ == "__main__":
    test_pruning()
    test_pruning_with_param_tying()
    test_pruning_by_flow()