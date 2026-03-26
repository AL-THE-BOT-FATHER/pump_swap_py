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

from constants import *          # WSOL, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM, EVENT_AUTH, GLOBAL_CONFIG, GLOBAL_VOL_ACC, FEE_PROGRAM, PF_AMM, PROTOCOL_FEE_RECIPIENT usw.
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

# ==================== KONSTANTEN & HELPER ====================
MAYHEM_FEE_RECIPIENTS = [
    Pubkey.from_string("GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"),
    Pubkey.from_string("4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6"),
    Pubkey.from_string("8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR"),
    Pubkey.from_string("4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH"),
    Pubkey.from_string("8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6"),
    Pubkey.from_string("Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk"),
    Pubkey.from_string("463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq"),
    Pubkey.from_string("6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA"),
]

def get_mayhem_fee_recipient() -> Tuple[Pubkey, Pubkey]:
    """Gibt (fee_recipient_pubkey, fee_recipient_wsol_ata) zurück"""
    recipient = random.choice(MAYHEM_FEE_RECIPIENTS)
    wsol_ata = get_associated_token_address(recipient, WSOL, TOKEN_PROGRAM_ID)
    return recipient, wsol_ata

def get_user_volume_accumulator(user: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PF_AMM)[0]

def get_pool_v2_pda(base_mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"pool-v2", bytes(base_mint)], PF_AMM)[0]


def calculate_anchor_discriminator(function_name: str) -> bytes:
    """Berechnet den Anchor Discriminator für eine Funktion"""
    import hashlib
    preimage = f"global:{function_name}"
    return hashlib.sha256(preimage.encode()).digest()[:8]

# --- INSTRUCTION DATA ---
def build_data(spendable_quote_in: int, min_base_out: int) -> bytes:
    discriminator = calculate_anchor_discriminator("buy_exact_quote_in")

    return (
        discriminator
        + struct.pack("<Q", spendable_quote_in)
        + struct.pack("<Q", min_base_out)
    )


# ==================== BUY ====================
def buy(
    client: Client,
    payer_keypair: Keypair,
    pair_address: str,
    sol_in: float = 0.1,
    slippage: int = 5,
    unit_budget: int = 200_000,
    unit_price: int = 1_000_000,

) -> bool:
    try:
        print(f"Starting BUY for {pair_address} | {sol_in} SOL | slippage {slippage}%")

        pool_keys: Optional[PoolKeys] = fetch_pool_keys(client, pair_address)
        if not pool_keys:
            print("Pool keys not found.")
            return False

        creator_vault_auth, creator_vault_ata = get_creator_vault_info(client, pool_keys.creator)
        if not creator_vault_auth or not creator_vault_ata:
            print("Creator vault info missing.")
            return False

        mint = pool_keys.base_mint
        token_info = client.get_account_info_json_parsed(mint).value
        base_token_program = token_info.owner
        decimals = token_info.data.parsed["info"]["decimals"]
        is_mayhem_mode = pool_keys.is_mayhem_mode
        is_cashback_coin = pool_keys.is_cashback_coin
        print(f"Token: {mint} | Decimals: {decimals} | Mayhem: {is_mayhem_mode} | Cashback: {is_cashback_coin}")

        base_reserve, quote_reserve = get_pool_reserves(client, pool_keys)
        raw_sol_in = int(sol_in * 1e9)
        base_amount_out = sol_for_tokens(raw_sol_in, base_reserve, quote_reserve)
        max_quote_in = int(raw_sol_in * (1 + slippage / 100))

        print(f"Expected out: {base_amount_out / 10**decimals:.6f} tokens | Max in: {max_quote_in / 1e9:.6f} SOL")

        # User Token Account (Base Mint)
        token_accounts = client.get_token_accounts_by_owner(payer_keypair.pubkey(), TokenAccountOpts(mint), Processed)
        if token_accounts.value:
            user_base_ata = token_accounts.value[0].pubkey
            create_ata_ix = None
        else:
            user_base_ata = get_associated_token_address(payer_keypair.pubkey(), mint, base_token_program)
            create_ata_ix = create_associated_token_account(payer_keypair.pubkey(), payer_keypair.pubkey(), mint, base_token_program)

        # WSOL Account (temporär)
        seed = base64.urlsafe_b64encode(os.urandom(24)).decode("utf-8")
        wsol_ata = Pubkey.create_with_seed(payer_keypair.pubkey(), seed, TOKEN_PROGRAM_ID)
        rent = Token.get_min_balance_rent_for_exempt_for_account(client)

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=payer_keypair.pubkey(),
                to_pubkey=wsol_ata,
                base=payer_keypair.pubkey(),
                seed=seed,
                lamports=int(rent + max_quote_in),
                space=165,
                owner=TOKEN_PROGRAM_ID,
            )
        )
        init_wsol_ix = initialize_account(
            InitializeAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_ata,
                mint=WSOL,
                owner=payer_keypair.pubkey(),
            )
        )

        # Fee Recipient Logic
        if is_mayhem_mode:
            fee_recipient, fee_recipient_ata = get_mayhem_fee_recipient()
        else:
            fee_recipient = PROTOCOL_FEE_RECIPIENT
            fee_recipient_ata = PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT   # oder dynamisch ableiten

        fee_config = derive_fee_config()
        user_vol_acc = get_user_volume_accumulator(payer_keypair.pubkey())
        pool_v2 = get_pool_v2_pda(mint)

        # === ACCOUNTS (23 Core + Remaining) ===
        keys = [
            AccountMeta(pubkey=pool_keys.amm, is_signer=False, is_writable=True),           # 1  pool
            AccountMeta(pubkey=payer_keypair.pubkey(), is_signer=True, is_writable=True),   # 2  user
            AccountMeta(pubkey=GLOBAL_CONFIG, is_signer=False, is_writable=False),          # 3  global
            AccountMeta(pubkey=pool_keys.base_mint, is_signer=False, is_writable=False),    # 4
            AccountMeta(pubkey=pool_keys.quote_mint, is_signer=False, is_writable=False),   # 5
            AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),           # 6
            AccountMeta(pubkey=wsol_ata, is_signer=False, is_writable=True),                # 7  user_quote (WSOL)
            AccountMeta(pubkey=pool_keys.pool_base_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_keys.pool_quote_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),          # 10 fee_recipient
            AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),       # 11 fee_ata
            AccountMeta(pubkey=base_token_program, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTH, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PF_AMM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),       # 18
            AccountMeta(pubkey=creator_vault_auth, is_signer=False, is_writable=False),     # 19
            AccountMeta(pubkey=GLOBAL_VOL_ACC, is_signer=False, is_writable=True),          # 20 global_vol
            AccountMeta(pubkey=user_vol_acc, is_signer=False, is_writable=True),            # 21 user_vol
            AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),             # 22
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False),            # 23
        ]

        # === REMAINING ACCOUNTS ===
        if is_cashback_coin:
            user_vol_wsol_ata = get_associated_token_address(user_vol_acc, WSOL, TOKEN_PROGRAM_ID)
            keys.append(AccountMeta(pubkey=user_vol_wsol_ata, is_signer=False, is_writable=True))

        keys.append(AccountMeta(pubkey=pool_v2, is_signer=False, is_writable=False))   # immer am Ende

        # === INSTRUCTION DATA (Buy) ===
        data = bytearray()
        data.extend(bytes.fromhex("66063d1201daebea"))          # buy discriminator
        data.extend(struct.pack("<Q", base_amount_out))
        data.extend(struct.pack("<Q", max_quote_in))
        # track_volume (OptionBool) kann bei Bedarf angehängt werden – meist [1,1] oder [1,0]

        swap_ix = Instruction(PF_AMM, build_data(10000, 1), keys)

        close_wsol_ix = close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_ata,
                dest=payer_keypair.pubkey(),
                owner=payer_keypair.pubkey(),
            )
        )

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
            create_wsol_ix,
            init_wsol_ix,
        ]
        if create_ata_ix:
            instructions.append(create_ata_ix)
        instructions.extend([swap_ix, close_wsol_ix])

        # Transaction bauen & senden
        recent_blockhash = client.get_latest_blockhash().value.blockhash
        message = MessageV0.try_compile(
            payer_keypair.pubkey(), instructions, [], recent_blockhash
        )
        tx = VersionedTransaction(message, [payer_keypair])

        # res = client.simulate_transaction(tx)
        # print(f"Simulate TX: {res}")
        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=False)).value
        print(f"Buy TX: {sig}")
        return confirm_txn(client, sig)

    except Exception as e:
        print(f"Buy error: {e}")
        return False


# ==================== SELL ====================
def sell(
    client: Client,
    payer_keypair: Keypair,
    pair_address: str,
    percentage: int = 100,
    slippage: int = 5,
    unit_budget: int = 200_000,
    unit_price: int = 1_000_000
) -> bool:
    try:
        print(f"Starting SELL for {pair_address} | {percentage}% | slippage {slippage}%")

        pool_keys = fetch_pool_keys(client, pair_address)
        if not pool_keys:
            return False

        creator_vault_auth, creator_vault_ata = get_creator_vault_info(client, pool_keys.creator)
        if not creator_vault_auth or not creator_vault_ata:
            return False

        mint = pool_keys.base_mint
        token_info = client.get_account_info_json_parsed(mint).value
        base_token_program = token_info.owner
        decimals = token_info.data.parsed["info"]["decimals"]
        is_mayhem_mode = pool_keys.is_mayhem_mode
        is_cashback_coin = pool_keys.is_cashback_coin
        print(f"Token: {mint} | Decimals: {decimals} | Mayhem: {is_mayhem_mode} | Cashback: {is_cashback_coin}")

        user_base_ata = get_associated_token_address(payer_keypair.pubkey(), mint, base_token_program)
        token_balance = get_token_balance(client, payer_keypair.pubkey(), mint)
        if token_balance == 0:
            print("No tokens to sell.")
            return False

        base_amount_in = int(token_balance * (percentage / 100))
        base_reserve, quote_reserve = get_pool_reserves(client, pool_keys)
        sol_out = tokens_for_sol(base_amount_in, base_reserve, quote_reserve)
        min_quote_out = int(sol_out * (1 - slippage / 100))

        # WSOL ATA für Output
        seed = base64.urlsafe_b64encode(os.urandom(24)).decode("utf-8")
        wsol_ata = Pubkey.create_with_seed(payer_keypair.pubkey(), seed, TOKEN_PROGRAM_ID)
        rent = Token.get_min_balance_rent_for_exempt_for_account(client)

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=payer_keypair.pubkey(),
                to_pubkey=wsol_ata,
                base=payer_keypair.pubkey(),
                seed=seed,
                lamports=int(rent),
                space=165,
                owner=TOKEN_PROGRAM_ID,
            )
        )
        init_wsol_ix = initialize_account(InitializeAccountParams(TOKEN_PROGRAM_ID, wsol_ata, WSOL, payer_keypair.pubkey()))

        if is_mayhem_mode:
            fee_recipient, fee_recipient_ata = get_mayhem_fee_recipient()
        else:
            fee_recipient = PROTOCOL_FEE_RECIPIENT
            fee_recipient_ata = PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT

        fee_config = derive_fee_config()
        user_vol_acc = get_user_volume_accumulator(payer_keypair.pubkey())
        pool_v2 = get_pool_v2_pda(mint)

        keys = [
            AccountMeta(pool_keys.amm, False, True),
            AccountMeta(payer_keypair.pubkey(), True, True),
            AccountMeta(GLOBAL_CONFIG, False, False),
            AccountMeta(pool_keys.base_mint, False, False),
            AccountMeta(pool_keys.quote_mint, False, False),
            AccountMeta(user_base_ata, False, True),
            AccountMeta(wsol_ata, False, True),
            AccountMeta(pool_keys.pool_base_token_account, False, True),
            AccountMeta(pool_keys.pool_quote_token_account, False, True),
            AccountMeta(fee_recipient, False, False),
            AccountMeta(fee_recipient_ata, False, True),
            AccountMeta(base_token_program, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM, False, False),
            AccountMeta(EVENT_AUTH, False, False),
            AccountMeta(PF_AMM, False, False),
            AccountMeta(creator_vault_ata, False, True),
            AccountMeta(creator_vault_auth, False, False),
            AccountMeta(fee_config, False, False),
            AccountMeta(FEE_PROGRAM, False, False),
        ]

        # Remaining für Cashback (bei Sell meist quote ATA + user_vol_acc)
        if is_cashback_coin:
            quote_mint = pool_keys.quote_mint
            quote_prog = TOKEN_PROGRAM_ID if quote_mint == WSOL else base_token_program  # anpassen falls nötig
            user_quote_vol_ata = get_associated_token_address(user_vol_acc, quote_mint, quote_prog)
            keys.append(AccountMeta(user_quote_vol_ata, False, True))
            keys.append(AccountMeta(user_vol_acc, False, True))

        keys.append(AccountMeta(pool_v2, False, False))   # immer am Ende

        # Sell Data
        data = bytearray()
        data.extend(bytes.fromhex("33e685a4017f83ad"))      # sell discriminator
        data.extend(struct.pack("<Q", base_amount_in))
        data.extend(struct.pack("<Q", min_quote_out))

        swap_ix = Instruction(PF_AMM, bytes(data), keys)

        close_wsol_ix = close_account(
            CloseAccountParams(TOKEN_PROGRAM_ID, wsol_ata, payer_keypair.pubkey(), payer_keypair.pubkey())
        )

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
            create_wsol_ix,
            init_wsol_ix,
            swap_ix,
            close_wsol_ix,
        ]

        if percentage == 100:
            close_token_ix = close_account(
                CloseAccountParams(base_token_program, user_base_ata, payer_keypair.pubkey(), payer_keypair.pubkey())
            )
            instructions.append(close_token_ix)

        recent = client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(payer_keypair.pubkey(), instructions, [], recent)
        tx = VersionedTransaction(msg, [payer_keypair])

        sig = client.send_transaction(tx, opts=TxOpts(skip_preflight=False)).value
        print(f"Buy TX: {sig}")
        return confirm_txn(client, sig)

    except Exception as e:
        print(f"Sell error: {e}")
        return False
