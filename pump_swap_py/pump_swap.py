import base64
import os
import struct
from typing import Optional

from solana.rpc.api import Client
from solana.rpc.commitment import Processed
from solana.rpc.types import TokenAccountOpts, TxOpts

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # type: ignore
from solders.instruction import AccountMeta, Instruction  # type: ignore
from solders.keypair import Keypair  # type: ignore
from solders.message import MessageV0  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.system_program import (
    CreateAccountWithSeedParams,
    create_account_with_seed,
)
from solders.transaction import VersionedTransaction  # type: ignore

from spl.token.client import Token
from spl.token.instructions import (
    CloseAccountParams,
    InitializeAccountParams,
    close_account,
    create_associated_token_account,
    get_associated_token_address,
    initialize_account,
)

from constants import *
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

def buy(client: Client, payer_keypair: Keypair, pair_address: str, sol_in: float = 0.1, slippage: int = 5, unit_budget: int = 150_000, unit_price: int = 1_000_000) -> bool:
    try:
        print(f"Starting buy transaction for pair address: {pair_address}")

        print("Fetching pool keys...")
        pool_keys: Optional[PoolKeys] = fetch_pool_keys(client, pair_address)
        
        if pool_keys is None:
            print("No pool keys found, aborting transaction.")
            return False
        print("Pool keys fetched successfully.")

        print("Fetching creator vault info...")
        creator_vault_authority, creator_vault_ata = get_creator_vault_info(client, pool_keys.creator)
        if creator_vault_authority is None or creator_vault_ata is None:
            print("No creator vault info found, aborting transaction.")
            return False
        print("Creator vault info fetched successfully.")

        mint = pool_keys.base_mint
        token_info = client.get_account_info_json_parsed(mint).value
        base_token_program = token_info.owner
        decimal = token_info.data.parsed['info']['decimals']

        print("Calculating transaction amounts...")
        sol_decimal = 1e9
        token_decimal = 10**decimal
        slippage_adjustment = 1 + (slippage / 100)
        max_quote_amount_in = int((sol_in * slippage_adjustment) * sol_decimal)

        base_reserve, quote_reserve = get_pool_reserves(client, pool_keys)
        raw_sol_in = int(sol_in * sol_decimal)
        base_amount_out = sol_for_tokens(raw_sol_in, base_reserve, quote_reserve)
        print(f"Max Quote Amount In: {max_quote_amount_in / sol_decimal} | Base Amount Out: {base_amount_out / token_decimal}")

        print("Checking for existing token account...")
        token_account_check = client.get_token_accounts_by_owner(payer_keypair.pubkey(), TokenAccountOpts(mint), Processed)
        
        if token_account_check.value:
            token_account = token_account_check.value[0].pubkey
            token_account_instruction = None
            print("Existing token account found.")
        else:
            token_account = get_associated_token_address(payer_keypair.pubkey(), mint, base_token_program)
            token_account_instruction = create_associated_token_account(payer_keypair.pubkey(), payer_keypair.pubkey(), mint, base_token_program)
            print("No existing token account found; creating associated token account.")

        print("Generating seed for WSOL account...")
        seed = base64.urlsafe_b64encode(os.urandom(24)).decode("utf-8")
        wsol_token_account = Pubkey.create_with_seed(payer_keypair.pubkey(), seed, TOKEN_PROGRAM_ID)
        balance_needed = Token.get_min_balance_rent_for_exempt_for_account(client)

        print("Creating and initializing WSOL account...")
        create_wsol_account_instruction = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=payer_keypair.pubkey(),
                to_pubkey=wsol_token_account,
                base=payer_keypair.pubkey(),
                seed=seed,
                lamports=int(balance_needed + max_quote_amount_in),
                space=ACCOUNT_SPACE,
                owner=TOKEN_PROGRAM_ID,
            )
        )

        init_wsol_account_instruction = initialize_account(
            InitializeAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_token_account,
                mint=WSOL,
                owner=payer_keypair.pubkey(),
            )
        )

        user_volume_accumulator = Pubkey.find_program_address([b"user_volume_accumulator", bytes(payer_keypair.pubkey())], PF_AMM)[0]
        fee_config = derive_fee_config()

        print("Creating swap instructions...")
        keys = [
            AccountMeta(pubkey=pool_keys.amm, is_signer=False, is_writable=True),
            AccountMeta(pubkey=payer_keypair.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=GLOBAL_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(pubkey=pool_keys.base_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=pool_keys.quote_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=wsol_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_keys.pool_base_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_keys.pool_quote_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=PROTOCOL_FEE_RECIPIENT, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=base_token_program, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTH, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PF_AMM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=creator_vault_authority, is_signer=False, is_writable=False),
            AccountMeta(pubkey=GLOBAL_VOL_ACC, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),
            AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        data = bytearray()
        data.extend(bytes.fromhex("66063d1201daebea"))
        data.extend(struct.pack('<Q', base_amount_out))
        data.extend(struct.pack('<Q', max_quote_amount_in))
        swap_instruction = Instruction(PF_AMM, bytes(data), keys)

        print("Preparing to close WSOL account after swap...")
        close_wsol_account_instruction = close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_token_account,
                dest=payer_keypair.pubkey(),
                owner=payer_keypair.pubkey(),
            )
        )

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
            create_wsol_account_instruction,
            init_wsol_account_instruction,
        ]

        if token_account_instruction:
            instructions.append(token_account_instruction)

        instructions.append(swap_instruction)
        instructions.append(close_wsol_account_instruction)
        
        print("Compiling transaction message...")
        compiled_message = MessageV0.try_compile(
            payer_keypair.pubkey(),
            instructions,
            [],
            client.get_latest_blockhash().value.blockhash,
        )

        print("Sending transaction...")
        txn_sig = client.send_transaction(
            txn=VersionedTransaction(compiled_message, [payer_keypair]),
            opts=TxOpts(skip_preflight=False)
        ).value
        print(f"Transaction Signature: {txn_sig}")
        
        print("Confirming transaction...")
        confirmed = confirm_txn(client, txn_sig)
        
        print(f"Transaction confirmed: {confirmed}")
        return confirmed
    except Exception as e:
        print("Error occurred during transaction:", e)
        return False

def sell(client: Client, payer_keypair: Keypair, pair_address: str, percentage: int = 100, slippage: int = 5, unit_budget: int = 150_000, unit_price: int = 1_000_000) -> bool:
    try:
        print(f"Starting sell transaction for pair address: {pair_address} with percentage: {percentage}%")
        
        print("Fetching pool keys...")
        pool_keys: Optional[PoolKeys] = fetch_pool_keys(client, pair_address)
        if pool_keys is None:
            print("No pool keys found, aborting transaction.")
            return False
        print("Pool keys fetched successfully.")

        print("Fetching creator vault info...")
        creator_vault_authority, creator_vault_ata = get_creator_vault_info(client, pool_keys.creator)
        if creator_vault_authority is None or creator_vault_ata is None:
            print("No creator vault info found, aborting transaction.")
            return False
        print("Creator vault info fetched successfully.")

        mint = pool_keys.base_mint
        token_info = client.get_account_info_json_parsed(mint).value
        base_token_program = token_info.owner
        decimal = token_info.data.parsed['info']['decimals']

        if not (1 <= percentage <= 100):
            print("Percentage must be between 1 and 100.")
            return False

        token_account = get_associated_token_address(payer_keypair.pubkey(), mint, base_token_program)

        print("Generating seed for WSOL account...")
        seed = base64.urlsafe_b64encode(os.urandom(24)).decode("utf-8")
        wsol_token_account = Pubkey.create_with_seed(payer_keypair.pubkey(), seed, TOKEN_PROGRAM_ID)
        balance_needed = Token.get_min_balance_rent_for_exempt_for_account(client)

        print("Creating and initializing WSOL account...")
        create_wsol_account_instruction = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=payer_keypair.pubkey(),
                to_pubkey=wsol_token_account,
                base=payer_keypair.pubkey(),
                seed=seed,
                lamports=int(balance_needed),
                space=ACCOUNT_SPACE,
                owner=TOKEN_PROGRAM_ID,
            )
        )

        init_wsol_account_instruction = initialize_account(
            InitializeAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_token_account,
                mint=WSOL,
                owner=payer_keypair.pubkey(),
            )
        )

        print("Retrieving token balance...")
        token_balance = get_token_balance(client, payer_keypair.pubkey(), mint)
        if token_balance == 0 or token_balance is None:
            print("Token balance is zero. Nothing to sell.")
            return False

        print("Calculating transaction amounts...")
        sol_decimal = 1e9
        token_decimal = 10**decimal
        base_amount_in = int(token_balance * (percentage / 100))
        base_reserve, quote_reserve = get_pool_reserves(client, pool_keys)
        sol_out = tokens_for_sol(base_amount_in, base_reserve, quote_reserve)
        slippage_adjustment = 1 - (slippage / 100)
        min_quote_amount_out = int((sol_out * slippage_adjustment))
        print(f"Base Amount In: {base_amount_in / token_decimal}, Minimum Quote Amount Out: {min_quote_amount_out / sol_decimal}")

        fee_config = derive_fee_config()

        print("Creating swap instructions...")    
        keys = [
            AccountMeta(pubkey=pool_keys.amm, is_signer=False, is_writable=True),
            AccountMeta(pubkey=payer_keypair.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=GLOBAL_CONFIG, is_signer=False, is_writable=False),
            AccountMeta(pubkey=pool_keys.base_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=pool_keys.quote_mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=wsol_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_keys.pool_base_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=pool_keys.pool_quote_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=PROTOCOL_FEE_RECIPIENT, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT, is_signer=False, is_writable=True),
            AccountMeta(pubkey=base_token_program, is_signer=False, is_writable=False),
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=EVENT_AUTH, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PF_AMM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True), 
            AccountMeta(pubkey=creator_vault_authority, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
            AccountMeta(pubkey=FEE_PROGRAM, is_signer=False, is_writable=False),
        ]

        data = bytearray()
        data.extend(bytes.fromhex("33e685a4017f83ad"))
        data.extend(struct.pack('<Q', base_amount_in))
        data.extend(struct.pack('<Q', min_quote_amount_out))
        
        swap_instruction = Instruction(PF_AMM, bytes(data), keys)

        print("Preparing to close WSOL account after swap...")
        close_wsol_account_instruction = close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=wsol_token_account,
                dest=payer_keypair.pubkey(),
                owner=payer_keypair.pubkey(),
            )
        )

        instructions = [
            set_compute_unit_limit(unit_budget),
            set_compute_unit_price(unit_price),
            create_wsol_account_instruction,
            init_wsol_account_instruction,
            swap_instruction,
            close_wsol_account_instruction
        ]

        if percentage == 100:
            print("Preparing to close token account after swap (selling 100%).")
            close_account_instruction = close_account(
                CloseAccountParams(
                    base_token_program, token_account, payer_keypair.pubkey(), payer_keypair.pubkey()
                )
            )
            instructions.append(close_account_instruction)

        print("Compiling transaction message...")
        compiled_message = MessageV0.try_compile(
            payer_keypair.pubkey(),
            instructions,
            [],
            client.get_latest_blockhash().value.blockhash,
        )

        print("Sending transaction...")
        txn_sig = client.send_transaction(
            txn=VersionedTransaction(compiled_message, [payer_keypair]),
            opts=TxOpts(skip_preflight=False)
        ).value
        print(f"Transaction Signature: {txn_sig}")
        
        print("Confirming transaction...")
        confirmed = confirm_txn(client, txn_sig)
        
        print(f"Transaction confirmed: {confirmed}")
        return confirmed
    except Exception as e:
        print("Error occurred during transaction:", e)
        return False