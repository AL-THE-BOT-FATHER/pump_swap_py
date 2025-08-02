from dataclasses import dataclass
from typing import List, Optional

from construct import Bytes, Int16ul, Int64ul, Int8ul, Padding, Struct

from solana.rpc.api import Client
from solana.rpc.commitment import Processed
from solana.rpc.types import MemcmpOpts, TokenAccountOpts

from solders.pubkey import Pubkey  # type: ignore
from solders.rpc.responses import RpcKeyedAccount  # type: ignore

from constants import PF_AMM, TOKEN_PROGRAM_ID, WSOL

POOL_LAYOUT = Struct(
    Padding(8),
    "pool_bump" / Int8ul,  # u8
    "index" / Int16ul,  # u16
    "creator" / Bytes(32),  # pubkey (32 bytes)
    "base_mint" / Bytes(32),  # pubkey (32 bytes)
    "quote_mint" / Bytes(32),  # pubkey (32 bytes)
    "lp_mint" / Bytes(32),  # pubkey (32 bytes)
    "pool_base_token_account" / Bytes(32),  # pubkey (32 bytes)
    "pool_quote_token_account" / Bytes(32),  # pubkey (32 bytes)
    "lp_supply" / Int64ul,  # u64
    "coin_creator" / Bytes(32),  # pubkey (32 bytes)
)

@dataclass
class PoolKeys:
    amm: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    pool_base_token_account: Pubkey
    pool_quote_token_account: Pubkey
    creator: Pubkey

def fetch_pool_keys(client: Client, pair_address: str):
    try:
        amm = Pubkey.from_string(pair_address)
        account_info = client.get_account_info_json_parsed(amm, commitment=Processed)
        amm_data = account_info.value.data
        decoded_data = POOL_LAYOUT.parse(amm_data)

        return PoolKeys(
            amm=amm,
            base_mint=Pubkey.from_bytes(decoded_data.base_mint),
            quote_mint=Pubkey.from_bytes(decoded_data.quote_mint),
            pool_base_token_account=Pubkey.from_bytes(decoded_data.pool_base_token_account),
            pool_quote_token_account=Pubkey.from_bytes(decoded_data.pool_quote_token_account),
            creator=Pubkey.from_bytes(decoded_data.coin_creator),
        )
    except:
        return None

def get_pool_reserves(client: Client, pool_keys: PoolKeys):
    try:
        
        base_vault = pool_keys.pool_base_token_account
        quote_vault = pool_keys.pool_quote_token_account # SOL
        
        balances_response = client.get_multiple_accounts_json_parsed(
            [base_vault, quote_vault], 
            Processed
        )
        
        balances = balances_response.value

        base_account = balances[0]
        quote_account = balances[1]
        
        base_account_balance = int(base_account.data.parsed['info']['tokenAmount']['amount'])
        quote_account_balance = int(quote_account.data.parsed['info']['tokenAmount']['amount'])
        
        if base_account_balance is None or quote_account_balance is None:
            return None, None
        
        return base_account_balance, quote_account_balance

    except Exception as e:
        print(f"Error occurred: {e}")
        return None, None

def fetch_pair_from_rpc(client: Client, base_str: str) -> Optional[str]:
    quote_str: str = "So11111111111111111111111111111111111111112"
    filters: List[List[MemcmpOpts]] = [
        [MemcmpOpts(offset=43, bytes=base_str), MemcmpOpts(offset=75, bytes=quote_str)],
        [MemcmpOpts(offset=43, bytes=quote_str), MemcmpOpts(offset=75, bytes=base_str)]
    ]
    pools: List[RpcKeyedAccount] = []
    for f in filters:
        try:
            resp = client.get_program_accounts(PF_AMM, filters=f)
            pools.extend(resp.value)
        except Exception as e:
            print(f"Error fetching program accounts with filters {f}: {e}")
            continue

    if not pools:
        return None

    best_pool_addr: Optional[str] = None
    max_liquidity: int = 0

    for pool in pools:
        try:
            pool_data: bytes = pool.account.data
            base_token_account: Pubkey = Pubkey.from_bytes(pool_data[139:171])
            quote_token_account: Pubkey = Pubkey.from_bytes(pool_data[171:203])
        except Exception as e:
            print(f"Error processing pool {pool.pubkey}: {e}")
            continue

        try:
            base_resp = client.get_token_account_balance(base_token_account)
            quote_resp = client.get_token_account_balance(quote_token_account)
        except Exception as e:
            print(f"Error fetching token account balance: {e}")
            continue

        if base_resp.value is None or quote_resp.value is None:
            continue

        try:
            base_balance: int = int(base_resp.value.amount)
            quote_balance: int = int(quote_resp.value.amount)
        except Exception as e:
            print(f"Error converting token balances to int: {e}")
            continue

        liquidity: int = base_balance * quote_balance
        if liquidity > max_liquidity:
            max_liquidity = liquidity
            best_pool_addr = str(pool.pubkey)
    return best_pool_addr

def sol_for_tokens(quote_amount_in, pool_base_token_reserves, pool_quote_token_reserves):
    base_amount_out = pool_base_token_reserves - (pool_base_token_reserves * pool_quote_token_reserves) // (pool_quote_token_reserves + quote_amount_in)
    return int(base_amount_out)

def tokens_for_sol(base_amount_in, pool_base_token_reserves, pool_quote_token_reserves):
    quote_amount_out = pool_quote_token_reserves - (pool_base_token_reserves * pool_quote_token_reserves) // (pool_base_token_reserves + base_amount_in)
    lp_fee = int(quote_amount_out * .002)
    protocol_fee = int(quote_amount_out * .0005)
    fees = lp_fee + protocol_fee
    return int(quote_amount_out - fees)

def get_creator_vault_info(client: Client, creator: Pubkey, mint: Pubkey) -> tuple[Pubkey|None, Pubkey|None]:
    try:
        creator_vault_authority = Pubkey.find_program_address([b"creator_vault", bytes(creator)], PF_AMM)[0]
        creator_vault_ata = client.get_token_accounts_by_owner_json_parsed(
            creator_vault_authority,
            TokenAccountOpts(
                mint=mint,
                program_id=TOKEN_PROGRAM_ID
            )
        ).value[0].pubkey
        return creator_vault_authority, creator_vault_ata
    except:
        return None, None