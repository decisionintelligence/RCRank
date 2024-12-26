import torch
from torch.utils.data import Dataset
import numpy as np
import json
import pandas as pd
import sys, os
from collections import deque
from utils import formatFilter, formatJoin, TreeNode, filterDict2Hist
from utils import *

class PlanTreeDataset(Dataset):
    def __init__(self, json_df : pd.DataFrame, train : pd.DataFrame, encoding, hist_file, card_norm, cost_norm, to_predict, table_sample):

        self.encoding = encoding
        
        self.length = len(json_df)
        
        nodes = [json.loads(plan)['Plan'] for plan in json_df]
        idxs = list(json_df.index)
        
    
        self.treeNodes = [] ## for mem collection
        self.collated_dicts = [self.js_node2dict(i,node) for i,node in zip(idxs, nodes)]

    def js_node2dict(self, idx, node):
        treeNode = self.traversePlan(node, idx, self.encoding)
        _dict = self.node2dict(treeNode)
        collated_dict = self.pre_collate(_dict)
        
        self.treeNodes.clear()
        del self.treeNodes[:]

        return collated_dict

    def __len__(self):
        return self.length
    
    def __getitem__(self, idx):
        
        return self.collated_dicts[idx]

    def old_getitem(self, idx):
        return self.dicts[idx], (self.cost_labels[idx], self.card_labels[idx])
      
    ## pre-process first half of old collator
    def pre_collate(self, the_dict, max_node = 100, rel_pos_max = 20):

        x = pad_2d_unsqueeze(the_dict['features'], max_node)
        N = len(the_dict['features'])
        attn_bias = torch.zeros([N+1,N+1], dtype=torch.float)
        
        edge_index = the_dict['adjacency_list'].t()
        if len(edge_index) == 0:
            shortest_path_result = np.array([[0]])
            path = np.array([[0]])
            adj = torch.tensor([[0]]).bool()
        else:
            adj = torch.zeros([N,N], dtype=torch.bool)
            adj[edge_index[0,:], edge_index[1,:]] = True
            
            shortest_path_result = floyd_warshall_rewrite(adj.numpy())
        
        rel_pos = torch.from_numpy((shortest_path_result)).long()

        
        attn_bias[1:, 1:][rel_pos >= rel_pos_max] = float('-inf')
        
        attn_bias = pad_attn_bias_unsqueeze(attn_bias, max_node + 1)
        rel_pos = pad_rel_pos_unsqueeze(rel_pos, max_node)

        heights = pad_1d_unsqueeze(the_dict['heights'], max_node)
        
        return {
            'x' : x,
            'attn_bias': attn_bias,
            'rel_pos': rel_pos,
            'heights': heights
        }


    def node2dict(self, treeNode):

        adj_list, num_child, features = self.topo_sort(treeNode)
        heights = self.calculate_height(adj_list, len(features))

        return {
            'features' : torch.FloatTensor(features),
            'heights' : torch.LongTensor(heights),
            'adjacency_list' : torch.LongTensor(np.array(adj_list)),
          
        }
    
    def topo_sort(self, root_node):
#        nodes = []
        adj_list = [] #from parent to children
        num_child = []
        features = []

        toVisit = deque()
        toVisit.append((0,root_node))
        next_id = 1
        while toVisit:
            idx, node = toVisit.popleft()
#            nodes.append(node)
            features.append(node.feature)
            num_child.append(len(node.children))
            for child in node.children:
                toVisit.append((next_id,child))
                adj_list.append((idx,next_id))
                next_id += 1
        
        return adj_list, num_child, features
    
    def traversePlan(self, plan, idx, encoding): # bfs accumulate plan

        nodeType = plan['Node Type']
        typeId = encoding.encode_type(nodeType)
        card = None #plan['Actual Rows']
        filters, alias = formatFilter(plan)
        join = formatJoin(plan)
        joinId = encoding.encode_join(join)
        filters_encoded = encoding.encode_filters(filters, alias)
        
        root = TreeNode(nodeType, typeId, filters, card, joinId, join, filters_encoded)
        
        self.treeNodes.append(root)

        if 'Relation Name' in plan:
            root.table = plan['Relation Name']
            root.table_id = encoding.encode_table(plan['Relation Name'])
        root.query_id = idx
        
        root.feature = node2feature(root, encoding, None, None)
        #    print(root)
        if 'Plans' in plan:
            for subplan in plan['Plans']:
                subplan['parent'] = plan
                node = self.traversePlan(subplan, idx, encoding)
                node.parent = root
                root.addChild(node)
        return root

    def calculate_height(self, adj_list,tree_size):
        if tree_size == 1:
            return np.array([0])

        adj_list = np.array(adj_list)
        node_ids = np.arange(tree_size, dtype=int)
        node_order = np.zeros(tree_size, dtype=int)
        uneval_nodes = np.ones(tree_size, dtype=bool)

        parent_nodes = adj_list[:,0]
        child_nodes = adj_list[:,1]

        n = 0
        while uneval_nodes.any():
            uneval_mask = uneval_nodes[child_nodes]
            unready_parents = parent_nodes[uneval_mask]

            node2eval = uneval_nodes & ~np.isin(node_ids, unready_parents)
            node_order[node2eval] = n
            uneval_nodes[node2eval] = False
            n += 1
        return node_order 



def node2feature(node, encoding, hist_file, table_sample):
    # type, join, filter123, mask123
    # 1, 1, 3x3 (9), 3
    num_filter = len(node.filterDict['colId'])
    pad = np.zeros((2,20-num_filter))
    filts = np.array(list(node.filterDict.values())) #cols, ops, vals
    ## 3x3 -> 9, get back with reshape 3,3
    filts = np.concatenate((filts, pad), axis=1).flatten() 
    mask = np.zeros(20)
    mask[:num_filter] = 1
    type_join = np.array([node.typeId, node.join])
    
    table = np.array([node.table_id])
    sample = np.zeros(1000)
    return np.concatenate((type_join, filts, mask, table, sample))