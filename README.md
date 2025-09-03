# pump_swap_py

Python library to trade on Pump Swap (AMM). 

```
pip install solana==0.36.1 solders==0.23.0
```

Updated: 9/2/2025

PumpSwap program (pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA):
- Buy instruction: Add accounts at indexes 21 (fee_config) and 22 (fee_program)
- Sell instruction: Add accounts at indexes 19 (fee_config) and 20 (fee_program)

# Contact

My Telegram: https://t.me/AL_THE_BOT_FATHER

Bot Mafia Telegram: https://t.me/Bot_Mafia_Support

<img width="200" height="200" alt="bot_mafia" src="https://github.com/user-attachments/assets/b0c8ca7c-83c0-45e9-8007-be85f13a4b0a" />


# Instructions

Clone the repo and use example_buy.py or example_sell.py.

**If you can - please support my work and donate to: 3pPK76GL5ChVFBHND54UfBMtg36Bsh1mzbQPTbcK89PD**


# FAQS

**What format should my private key be in?** 

The private key should be in the base58 string format, not bytes. 

**Why are my transactions being dropped?** 

You get what you pay for. Don't use the main-net RPC, just spend the money for Helius or Quick Node.

**How do I change the fee?** 

Modify the unit_budget and unit_price values. 

**Does this code work on devnet?**

No. 


# Example

```
from solana.rpc.api import Client
from solders.keypair import Keypair  # type: ignore

from pool_utils import fetch_pair_from_rpc
from pump_swap import buy

# Configuration
priv_key = "base58_priv_str_here"
rpc = "rpc_url_here"
mint_str = "pump_swap_address"
sol_in = 0.1
slippage = 5
unit_budget = 150_000
unit_price = 1_000_000

# Initialize client and keypair
client = Client(rpc)
payer_keypair = Keypair.from_base58_string(priv_key)

# Fetch pair and execute buy
pair_address = fetch_pair_from_rpc(client, mint_str)
if pair_address:
    buy(client, payer_keypair, pair_address, sol_in, slippage, unit_budget, unit_price)
else:
    print("No pair address found...")

```

```
from solana.rpc.api import Client
from solders.keypair import Keypair  # type: ignore

from pool_utils import fetch_pair_from_rpc
from pump_swap import sell

# Configuration
priv_key = "base58_priv_str_here"
rpc = "rpc_url_here"
mint_str = "pump_swap_address"
percentage = 100
slippage = 5
unit_budget = 150_000
unit_price = 1_000_000

# Initialize client and keypair
client = Client(rpc)
payer_keypair = Keypair.from_base58_string(priv_key)

# Fetch pair and execute buy
pair_address = fetch_pair_from_rpc(client, mint_str)

if pair_address:
    sell(client, payer_keypair, pair_address, percentage, slippage, unit_budget, unit_price)
else:
    print("No pair address found...")

```

# Contributors

https://github.com/Jarki
