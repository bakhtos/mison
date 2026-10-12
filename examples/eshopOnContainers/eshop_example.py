import json


# # Mine commits corresponding to v3.0.0 of eShopOnContainers
# from mison.miner import PydrillerMiner, CommitJSONEncoder
# import datetime
#
# miner = PydrillerMiner(repo="https://github.com/M3SOulu/ECSA2024-eShopOnContainers",
#                               to=datetime.datetime.fromisoformat("2019-11-26"),
#                               since=datetime.datetime.fromisoformat("2019-03-21"))
# data = miner.mine_commits()
# with open("eshop_commits_3.0.0.json", 'w') as f:
#     json.dump(data, f, cls=CommitJSONEncoder, indent=4)

# CLI equivalent:
# mison commit --backend pydriller --repo https://github.com/M3SOulu/ECSA2024-eShopOnContainers
# --to 2019-11-26
# --since 2019-03-21
# --commit_json eshop_commits_3.0.0.json


## Alternative: load already mined commits
from mison.miner import CommitJSONDecoder
with open("eshop_commits_3.0.0.json", 'r') as f:
    data = json.load(f, cls=CommitJSONDecoder)


## Make the Dev to File network
from mison.networks import DevFileMapping
file_network = DevFileMapping(data)


## Map developers
with open("email_map.json", 'r') as f:
    email_map = json.load(f)

file_network.map_developers(email_map)


## Traces the renaming of files across commits (WARNING: experimental, can cause an infinite loop)
file_network.map_renamed_files()

## Compute developer collaboration on file level (can also be done on components)
from mison.networks.collaboration import CountCollaboration, CosineCollaboration

# Count collaboration (Li et al.)
count_collaboration_files = CountCollaboration(file_network)
count_collaboration_files.to_json("eshop_3.0.0_count_collab_file.json")

# Cosine similarity (Jermakovich et al.)
cosine_collaboration_files = CosineCollaboration(file_network)
cosine_collaboration_files.to_json("eshop_3.0.0_cosine_collab_file.json")


## Make the Dev to Component network
from eshop_component_mapping import component_mapping as eshop_mapping
from mison.networks import DevComponentMapping
component_network = DevComponentMapping(file_network, eshop_mapping)


## Compute microservice coupling
from mison.networks.coupling import LogicalCoupling, OrganizationalCoupling

logical_coupling_ms =  LogicalCoupling(component_network)
logical_coupling_ms.to_json("eshop_3.0.0_logical_ms.json")
org_coupling_ms = OrganizationalCoupling(component_network)
org_coupling_ms.to_json("eshop_3.0.0_org_ms.json")

## Extra: compute developer collaboration on components
count_collaboration_files = CountCollaboration(component_network)
count_collaboration_files.to_json("eshop_3.0.0_count_collab_ms.json")
cosine_collaboration_files = CosineCollaboration(component_network)
cosine_collaboration_files.to_json("eshop_3.0.0_cosine_collab_ms.json")

