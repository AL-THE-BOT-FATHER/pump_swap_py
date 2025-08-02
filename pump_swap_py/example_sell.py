from solana.rpc.api import Client
from solders.keypair import Keypair  # type: ignore

from pool_utils import fetch_pair_from_rpc
from pump_swap import sell

# Configuration
priv_key = ""
rpc = ""
mint_str = "pump_swap_address"
percentage = 100
slippage = 5
unit_budget = 150_000
unit_price = 1_000_000

# Initialize client and keypair
client = Client(rpc)
payer_keypair = Keypair.from_base58_string(priv_key)

# Fetch pair and execute buy
# pair_address = fetch_pair_from_rpc(client, mint_str)
pair_address = "539m4mVWt6iduB6W8rDGPMarzNCMesuqY5eUTiiYHAgR" # Pump
# pair_address = "3MYdZA4KVa6UeHNowVYVRfDbMD8FcnXexpdePb6Pm9B1" # ! Inversed Warn if liquidity

if pair_address:
    sell(client, payer_keypair, pair_address, percentage, slippage, unit_budget, unit_price)
else:
    print("No pair address found...")
