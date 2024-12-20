from __future__ import annotations

"""
Copyright 2023-2024 SGLang Team
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""
The radix tree data structure for managing the KV cache.
"""

import heapq
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Callable, List, Optional

import torch

from sglang.srt.mem_cache.base_prefix_cache import BasePrefixCache
from sglang.srt.mem_cache.memory_pool import BaseTokenToKVPool, ReqToTokenPool


if TYPE_CHECKING:
    from sglang.srt.managers.schedule_batch import Req


class TreeNode:
    def __init__(self):
        self.children = defaultdict(TreeNode)
        self.parent = None
        self.key = None
        self.value = None
        self.lock_ref = 0
        self.last_access_time = time.time()
        self.ref_counter = 0

    def __lt__(self, other: "TreeNode"):
        return self.last_access_time < other.last_access_time


def _key_match(key0: List, key1: List):
    i = 0
    for k0, k1 in zip(key0, key1):
        if k0 != k1:
            break
        i += 1
    return i


class RadixCache(BasePrefixCache):
    def __init__(
        self,
        req_to_token_pool: ReqToTokenPool,
        token_to_kv_pool: BaseTokenToKVPool,
        flag: bool = False,
        disable: bool = False,
    ):
        self.req_to_token_pool = req_to_token_pool
        self.token_to_kv_pool = token_to_kv_pool
        self.disable = disable
        self.reset()
        self.call_counter = 0
        self.token_counter = 0
        self.flag = flag

    ##### Public API #####

    def reset(self):
        self.root_node = TreeNode()
        self.root_node.key = []
        self.root_node.value = []
        self.root_node.lock_ref = 1
        self.evictable_size_ = 0

    def match_prefix(self, key: List, **kwargs):
        if self.disable:
            return [], self.root_node
        #added for testing

        value = []
        last_node = [self.root_node]
        self._match_prefix_helper(self.root_node, key, value, last_node)
        if value:
            value = torch.concat(value)
        else:
            value = torch.tensor([], dtype=torch.int32)
        return value, last_node[0]

    def insert(self, key: List, value=None):
        if self.disable:
            return 0

        if value is None:
            value = [x for x in key]
        return self._insert_helper(self.root_node, key, value)

    def cache_finished_req(self, req: Req, token_ids: Optional[List[int]] = None):
        """Cache request when it finishes."""
        if token_ids is None:
            token_ids = (req.origin_input_ids + req.output_ids)[:-1]
        kv_indices = self.req_to_token_pool.req_to_token[
            req.req_pool_idx, : len(token_ids)
        ]

        if self.disable:
            self.token_to_kv_pool.free(kv_indices)
            self.req_to_token_pool.free(req.req_pool_idx)
            return

        # Radix Cache takes one ref in memory pool
        new_prefix_len = self.insert(token_ids, kv_indices.clone())
        self.token_to_kv_pool.free(kv_indices[len(req.prefix_indices) : new_prefix_len])

        # Remove req slot release the cache lock
        self.req_to_token_pool.free(req.req_pool_idx)
        self.dec_lock_ref(req.last_node)

    def cache_unfinished_req(self, req: Req, token_ids: Optional[List[int]] = None):
        """Cache request when it is unfinished."""
        if self.disable:
            return

        if token_ids is None:
            token_ids = req.fill_ids

        kv_indices = self.req_to_token_pool.req_to_token[
            req.req_pool_idx, : len(token_ids)
        ]

        # Radix Cache takes one ref in memory pool
        new_prefix_len = self.insert(token_ids, kv_indices.clone())
        self.token_to_kv_pool.free(kv_indices[len(req.prefix_indices) : new_prefix_len])

        # The prefix indices could be updated, reuse it
        new_indices, new_last_node = self.match_prefix(token_ids)
        assert len(new_indices) == len(token_ids)
        self.req_to_token_pool.req_to_token[
            req.req_pool_idx, len(req.prefix_indices) : len(new_indices)
        ] = new_indices[len(req.prefix_indices) :]

        self.dec_lock_ref(req.last_node)
        self.inc_lock_ref(new_last_node)
        req.prefix_indices = new_indices
        req.last_node = new_last_node

    def pretty_print(self):
        self._print_helper(self.root_node, 0)
        print(f"#tokens: {self.total_size()}")

    def total_size(self):
        return self._total_size_helper(self.root_node)

    def evict(self, num_tokens: int, evict_callback: Callable, wait_tensor: List[int]=None, it_counter: int = None):
        self.flag = True
        self.call_counter = self.call_counter+1
        
        print(f' iteration counter = {it_counter}, _________Evict is called in sglang_______________', flush=True)
        # f = open("sglang_cache_hit_rate_trace_inside_evict_update.txt", "a")
        # f = open("lookahead_cache_hit_rate_trace.txt", "a")
        # f.write(f'counter = {it_counter}\t')
        # f.write(f'evict called for {self.call_counter} timese, evict target = {num_tokens} tokens\t')

        print(f'evict called for {self.call_counter} timese, evict target = {num_tokens} tokens\t', flush=True)
       
        if self.disable:
            return

        leaves = self._collect_leaves()
        #print(f'tree size = = {len(leaves)}',flush = True)
        heapq.heapify(leaves)

        num_evicted = 0
        while num_evicted < num_tokens and len(leaves):
            x = heapq.heappop(leaves)
            #print(f'token length = {len(x.value)}')
            # print(f'printing type of x = node to check {type(x.value)} and the value is {x.value}')

            if x == self.root_node:
                break
            if x.lock_ref > 0:
                #print("second if")
                continue

            evict_callback(x.value)
            # f.write(f'\n evicted {len(x.value)} tokens \n')
            num_evicted += len(x.value)
            self._delete_leaf(x)

            if len(x.parent.children) == 0:
                heapq.heappush(leaves, x.parent)

        self.token_counter = self.token_counter + num_evicted
        # f.write(f'\n evicted at iteration {it_counter} round = {num_evicted},  Total evicted {self.token_counter} tokens\n\n')
        # f.close()
        print(f'evict called for {self.call_counter} timese, Number of evicted tokens = {num_evicted} tokens, Total evicted = {self.token_counter} tokens', flush = True)



    def evict1(self, num_tokens: int, evict_callback: Callable, wait_tensor: List[Req]=None, it_counter: int = None):
        self.flag = True
        self.call_counter = self.call_counter+1
        print(f' iteration counter = {it_counter}, _________Evict is called in lookahead_______________', flush=True)
        # f = open("sglang_cache_hit_rate_trace_w_tok_hit_num.txt", "a")
        # f = open("lookahead_cache_hit_rate_trace_inside evict_update.txt", "a")
        # f.write(f'counter = {it_counter}\t')
        # f.write(f'evict called for {self.call_counter} timese, evict target = {num_tokens} tokens\t')
        
        print(f'evict called for {self.call_counter} timese, evict target = {num_tokens} tokens\t', flush=True)
        # item_to_check_tensor = []
        
        # for i in wait_tensor:
        #     item_to_check_tensor.append(torch.tensor(i.input_ids, dtype=torch.int32))

        if self.disable:
            return

        leaves = self._collect_leaves()
        # heapq.heapify(leaves)
        print(f'tree size = = {len(leaves)}',flush = True)
        num_evicted = 0
        
        while num_evicted < num_tokens and len(leaves):
            x = heapq.heappop(leaves)

            if x == self.root_node:
                break
            if x.lock_ref > 0:
                continue

            if x not in wait_tensor:
                evict_callback(x.value)
                # f.write(f'\n evicted {len(x.value)} tokens \n')
                num_evicted += len(x.value)
                self._delete_leaf(x)
                    
                if len(x.parent.children) == 0:
                    heapq.heappush(leaves, x.parent)    
        
        # for x in leaves:
        #     #print(f'token length = {len(x.value)}')
        #     #print(f'length = {len(item_to_check_tensor)}')

        #     if x not in item_to_check_tensor:
        #         # print("Entering main evict")
        #         if x == self.root_node:
        #             # print("break from here 1")
        #             break
        #         if x.lock_ref > 0:
        #             #print("second if")
        #             continue

        #         evict_callback(x.value)
        #         # f.write(f'\n evicted {len(x.value)} tokens \n')
        #         num_evicted += len(x.value)
        #         self._delete_leaf(x)
        #         if len(x.parent.children) == 0:
        #             leaves.append(x.parent)
        #         if num_evicted >= num_tokens:
        #             # print("break from here 2")
        #             break
            else:
                print("here x is found in the item to check", flush = True)
                continue

        self.token_counter = self.token_counter + num_evicted
        # f.write(f'\n evicted at iteration {it_counter} round = {num_evicted},  Total evicted {self.token_counter} tokens\n\n')
        # f.close()
        print(f'evict called for {self.call_counter} timese, Number of evicted tokens = {num_evicted} tokens, Total evicted = {self.token_counter} tokens', flush = True)



    def inc_lock_ref(self, node: TreeNode):
        if self.disable:
            return 0

        delta = 0
        while node != self.root_node:
            if node.lock_ref == 0:
                self.evictable_size_ -= len(node.value)
                delta -= len(node.value)
            node.lock_ref += 1
            node = node.parent
        return delta

    def dec_lock_ref(self, node: TreeNode):
        if self.disable:
            return 0

        delta = 0
        while node != self.root_node:
            if node.lock_ref == 1:
                self.evictable_size_ += len(node.value)
                delta += len(node.value)
            node.lock_ref -= 1
            node = node.parent
        return delta

    def evictable_size(self):
        return self.evictable_size_

    ##### Internal Helper Functions #####

    def _match_prefix_helper(
        self, node: TreeNode, key: List, value, last_node: TreeNode
    ):
        node.last_access_time = time.time()
        if len(key) == 0:
            return

        if key[0] in node.children.keys():
            child = node.children[key[0]]
            prefix_len = _key_match(child.key, key)
            if prefix_len < len(child.key):
                new_node = self._split_node(child.key, child, prefix_len)
                value.append(new_node.value)
                last_node[0] = new_node
            else:
                value.append(child.value)
                last_node[0] = child
                self._match_prefix_helper(child, key[prefix_len:], value, last_node)

    def _split_node(self, key, child: TreeNode, split_len: int):
        # new_node -> child
        new_node = TreeNode()
        new_node.children = {key[split_len:][0]: child}
        new_node.parent = child.parent
        new_node.lock_ref = child.lock_ref
        new_node.key = child.key[:split_len]
        new_node.value = child.value[:split_len]
        child.parent = new_node
        child.key = child.key[split_len:]
        child.value = child.value[split_len:]
        new_node.parent.children[key[:split_len][0]] = new_node
        return new_node

    def _insert_helper(self, node: TreeNode, key: List, value):
        node.last_access_time = time.time()
        if len(key) == 0:
            return 0

        if key[0] in node.children.keys():
            child = node.children[key[0]]
            prefix_len = _key_match(child.key, key)

            if prefix_len == len(child.key):
                if prefix_len == len(key):
                    return prefix_len
                else:
                    key = key[prefix_len:]
                    value = value[prefix_len:]
                    return prefix_len + self._insert_helper(child, key, value)

            new_node = self._split_node(child.key, child, prefix_len)
            return prefix_len + self._insert_helper(
                new_node, key[prefix_len:], value[prefix_len:]
            )

        if len(key):
            new_node = TreeNode()
            new_node.parent = node
            new_node.key = key
            new_node.value = value
            node.children[key[0]] = new_node
            self.evictable_size_ += len(value)
        return 0

    def _print_helper(self, node: TreeNode, indent: int):
        for _, child in node.children.items():
            print(" " * indent, len(child.key), child.key[:10], f"r={child.lock_ref}")
            self._print_helper(child, indent=indent + 2)

    def _delete_leaf(self, node):
        for k, v in node.parent.children.items():
            if v == node:
                break
        del node.parent.children[k]
        self.evictable_size_ -= len(node.key)

    def _total_size_helper(self, node: TreeNode):
        x = len(node.value)
        for child in node.children.values():
            x += self._total_size_helper(child)
        return x

    def _collect_leaves(self):
        ret_list = []
        stack = [self.root_node]

        while stack:
            cur_node = stack.pop()
            if len(cur_node.children) == 0:
                ret_list.append(cur_node)
            else:
                stack.extend(cur_node.children.values())

        return ret_list


if __name__ == "__main__":
    tree = RadixCache(None, None, False)

    tree.insert("Hello")
    tree.insert("Hello")
    tree.insert("Hello_L.A.!")
    # tree.insert("Hello_world! Happy")
    # tree.insert("I love you!")
    tree.pretty_print()

    # print(tree.match_prefix("I love you! aha"))

    # def evict_callback(x):
    #    print("evict", x)
    #    return len(x)

    # tree.evict(5, evict_callback)
    # tree.evict(10, evict_callback)
    # tree.pretty_print()