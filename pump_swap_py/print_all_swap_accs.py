import base64
import os
import random
import struct
from typing import Optional, Tuple
from solana.rpc.api import Client
from solana.rpc.commitment import Processed
from solana.rpc.types import TokenAccountOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.system_program import CreateAccountWithSeedParams, create_account_with_seed
from solders.transaction import VersionedTransaction
from spl.token.client import Token
from spl.token.instructions import (
    CloseAccountParams,
    InitializeAccountParams,
    close_account,
    create_associated_token_account,
    get_associated_token_address,
    initialize_account,
)
from constants import *  # WSOL, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM, EVENT_AUTH,
# GLOBAL_CONFIG, GLOBAL_VOL_ACC, FEE_PROGRAM, PF_AMM, PROTOCOL_FEE_RECIPIENT, etc.
from common_utils import confirm_txn, get_token_balance
from pool_utils import (
    PoolKeys,
    fetch_pool_keys,
    get_creator_vault_info,
    get_pool_reserves,
    tokens_for_sol,
    sol_for_tokens,
    derive_fee_config,
)


def print_maximal_swap_accounts(
    client: Client,
    pool_address: str,
    payer: Optional[Pubkey] = None,
    is_buy: bool = True,
) -> None:
    """
    Prints the MAXIMUM possible account list for a PumpSwap Buy or Sell transaction
    (including all conditional accounts: Mayhem Mode, Cashback, Volume Accumulator,
    Pool-V2, etc.).

    Use this function for debugging and to fully understand the complete instruction
    structure required by the on-chain program.
    """
    print(f"\n{'='*100}")
    print(f"MAXIMUM ACCOUNT LIST FOR {'BUY' if is_buy else 'SELL'}")
    print(f"Pool: {pool_address}")
    if payer:
        print(f"Payer: {payer}")
    print(f"{'='*100}\n")

    # 1. Fetch Pool Keys
    pool_keys = fetch_pool_keys(client, pool_address)
    if not pool_keys:
        print("Error: Could not fetch pool keys!")
        return

    # 2. Get Creator Vault info
    creator_vault_auth, creator_vault_ata = get_creator_vault_info(client, pool_keys.creator)
    if not creator_vault_auth or not creator_vault_ata:
        print("Warning: Could not determine Creator Vault.")

    # 3. Basic Info
    base_mint = pool_keys.base_mint
    quote_mint = pool_keys.quote_mint
    base_token_program = client.get_account_info_json_parsed(base_mint).value.owner
    quote_token_program = TOKEN_PROGRAM_ID if quote_mint == WSOL else base_token_program
    is_wsol_pool = quote_mint == WSOL

    # For demonstration - in production you should check actual pool data
    is_cashback_possible = True
    is_mayhem_possible = True

    # 4. Calculate all required PDAs
    pool_v2_pda = Pubkey.find_program_address([b"pool-v2", bytes(base_mint)], PF_AMM)[0]
    global_vol_acc = GLOBAL_VOL_ACC

    user_vol_acc = None
    user_vol_wsol_ata = None
    user_vol_quote_ata = None

    if payer:
        user_vol_acc = Pubkey.find_program_address([b"user_volume_accumulator", bytes(payer)], PF_AMM)[0]
        user_vol_wsol_ata = get_associated_token_address(user_vol_acc, WSOL, TOKEN_PROGRAM_ID)
        user_vol_quote_ata = get_associated_token_address(user_vol_acc, quote_mint, quote_token_program)

    # Mayhem Mode - random recipient for demonstration
    mayhem_fee_recipient = random.choice([
        Pubkey.from_string("GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"),
        Pubkey.from_string("4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6"),
        # ... add all 8 mayhem recipients here
    ])
    mayhem_fee_ata = get_associated_token_address(mayhem_fee_recipient, WSOL, TOKEN_PROGRAM_ID)

    # 5. Build maximum account list (matching current PumpSwap program)
    account_entries = []
    idx = 1

    # === CORE ACCOUNTS (always present) ===
    account_entries.append((idx, "pool_id", pool_keys.amm, False, True, "Pool Account (writable)")); idx += 1
    account_entries.append((idx, "user", payer or Pubkey.default(), True, True, "User (signer + writable)")); idx += 1
    account_entries.append((idx, "global", GLOBAL_CONFIG, False, False, "Global PDA (readonly)")); idx += 1
    account_entries.append((idx, "base_mint", base_mint, False, False, "Base Mint (readonly)")); idx += 1
    account_entries.append((idx, "quote_mint", quote_mint, False, False, "Quote Mint (readonly)")); idx += 1
    account_entries.append((idx, "user_base_token_account", 
                           "→ get_associated_token_address(payer, base_mint, base_token_program)", 
                           False, True, "User Base ATA")); idx += 1
    account_entries.append((idx, "user_quote_token_account", 
                           "→ WSOL ATA or Quote ATA", 
                           False, True, "User Quote ATA (WSOL for Buy)")); idx += 1
    account_entries.append((idx, "pool_base_token_account", pool_keys.pool_base_token_account, False, True, "Pool Base Vault")); idx += 1
    account_entries.append((idx, "pool_quote_token_account", pool_keys.pool_quote_token_account, False, True, "Pool Quote Vault")); idx += 1

    # Fee Recipient (Mayhem or normal)
    if is_mayhem_possible:
        account_entries.append((idx, "fee_recipient (MAYHEM)", mayhem_fee_recipient, False, False, "Mayhem Fee Recipient (random)")); idx += 1
        account_entries.append((idx, "fee_recipient_ata (MAYHEM)", mayhem_fee_ata, False, True, "Mayhem WSOL ATA")); idx += 1
    else:
        account_entries.append((idx, "fee_recipient", PROTOCOL_FEE_RECIPIENT, False, False, "Protocol Fee Recipient")); idx += 1
        account_entries.append((idx, "fee_recipient_ata", PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT, False, True, "Protocol Fee ATA")); idx += 1

    account_entries.append((idx, "base_token_program", base_token_program, False, False, "Base Token Program")); idx += 1
    account_entries.append((idx, "quote_token_program", quote_token_program, False, False, "Quote Token Program")); idx += 1
    account_entries.append((idx, "system_program", SYSTEM_PROGRAM_ID, False, False, "System Program")); idx += 1
    account_entries.append((idx, "associated_token_program", ASSOCIATED_TOKEN_PROGRAM, False, False, "Associated Token Program")); idx += 1
    account_entries.append((idx, "event_authority", EVENT_AUTH, False, False, "Event Authority")); idx += 1
    account_entries.append((idx, "amm_program", PF_AMM, False, False, "PumpSwap Program")); idx += 1
    account_entries.append((idx, "coin_creator_vault_ata", creator_vault_ata, False, True, "Creator Vault ATA")); idx += 1
    account_entries.append((idx, "coin_creator_vault_authority", creator_vault_auth, False, False, "Creator Vault Authority")); idx += 1

    # === VOLUME ACCUMULATOR ACCOUNTS ===
    account_entries.append((idx, "global_volume_accumulator", global_vol_acc, False, True, "Global Volume Accumulator")); idx += 1

    if payer:
        account_entries.append((idx, "user_volume_accumulator", user_vol_acc, False, True, "User Volume Accumulator")); idx += 1
    else:
        account_entries.append((idx, "user_volume_accumulator", "→ PDA(payer)", False, True, "User Volume Accumulator (PDA)")); idx += 1

    account_entries.append((idx, "fee_config", derive_fee_config(), False, False, "Fee Config PDA")); idx += 1
    account_entries.append((idx, "fee_program", FEE_PROGRAM, False, False, "Fee Program")); idx += 1

    # === REMAINING ACCOUNTS (conditional) ===
    if is_cashback_possible and payer:
        account_entries.append((idx, "user_volume_accumulator_WSOL_ATA (cashback)", user_vol_wsol_ata, False, True, "Cashback WSOL ATA (for Buy)")); idx += 1
        account_entries.append((idx, "user_volume_accumulator_quote_ATA (cashback)", user_vol_quote_ata, False, True, "Cashback Quote ATA (for Sell)")); idx += 1

    account_entries.append((idx, "pool_v2_pda", pool_v2_pda, False, False, "Pool V2 PDA (must be the LAST remaining account!)")); idx += 1

    # 6. Pretty Print
    print(f"{'Idx':<4} {'Name':<45} {'Pubkey (short)':<44} {'Signer':<7} {'Writable':<9} Note")
    print("-" * 130)

    for i, name, pubkey, signer, writable, note in account_entries:
        if isinstance(pubkey, Pubkey):
            pk_str = str(pubkey)[:8] + "..." + str(pubkey)[-8:]
        else:
            pk_str = str(pubkey)[:40]
        print(f"{i:<4} {name:<45} {pk_str:<44} {str(signer):<7} {str(writable):<9} {note}")

    print(f"\nTotal possible accounts (maximum): {len(account_entries)}")
    print("\nNotes:")
    print("• Mayhem Mode   → Fee Recipient + ATA are replaced with a random Mayhem recipient")
    print("• Cashback Coin → Additional UserVolumeAccumulator ATAs + PDA are included")
    print("• Pool-V2 PDA   → Must always be the very last remaining account")
    print("• On Sell transactions, different volume accounts may be used (quote_mint instead of WSOL)")
    print(f"{'='*100}\n")


# =============================================
# Example Usage (copy into your script)
# =============================================
if __name__ == "__main__":
    from solders.keypair import Keypair

    RPC_URL = "-- insert your RPC URL here --"
    PRIVATE_KEY = "-- insert your private key here --"  # Base58

    client = Client(RPC_URL)
    test_pool = "-- insert your pool address here --"   # Example PumpSwap AMM address
    test_payer = Keypair.from_base58_string(PRIVATE_KEY).pubkey()

    print_maximal_swap_accounts(client, test_pool, payer=test_payer, is_buy=True)
