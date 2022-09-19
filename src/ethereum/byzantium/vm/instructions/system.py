"""
Ethereum Virtual Machine (EVM) System Instructions
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. contents:: Table of Contents
    :backlinks: none
    :local:

Introduction
------------

Implementations of the EVM system related instructions.
"""
from ethereum.base_types import U256, Bytes0, Uint
from ethereum.utils.ensure import ensure

from ...eth_types import Address
from ...state import (
    account_has_code_or_nonce,
    get_account,
    increment_nonce,
    is_account_alive,
    set_account_balance,
)
from ...utils.address import compute_contract_address, to_address
from .. import Evm, Message
from ..exceptions import Revert, WriteInStaticContext
from ..gas import (
    GAS_CALL,
    GAS_CALL_VALUE,
    GAS_CREATE,
    GAS_NEW_ACCOUNT,
    GAS_SELF_DESTRUCT,
    GAS_SELF_DESTRUCT_NEW_ACCOUNT,
    GAS_ZERO,
    calculate_call_gas_cost,
    calculate_message_call_gas_stipend,
    charge_gas,
    max_message_call_gas,
)
from ..memory import extend_memory, memory_read_bytes, memory_write
from ..stack import pop, push


def create(evm: Evm) -> None:
    """
    Creates a new account with associated code.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # This import causes a circular import error
    # if it's not moved inside this method
    from ...vm.interpreter import STACK_DEPTH_LIMIT, process_create_message

    # STACK
    endowment = pop(evm.stack)
    memory_start_position = pop(evm.stack)
    memory_size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_start_position, memory_size)
    charge_gas(evm, GAS_CREATE)

    create_message_gas = max_message_call_gas(Uint(evm.gas_left))
    evm.gas_left -= U256(create_message_gas)

    # OPERATION
    ensure(not evm.message.is_static, WriteInStaticContext)
    evm.return_data = b""

    sender_address = evm.message.current_target
    sender = get_account(evm.env.state, sender_address)

    contract_address = compute_contract_address(
        evm.message.current_target,
        get_account(evm.env.state, evm.message.current_target).nonce,
    )

    if (
        sender.balance < endowment
        or sender.nonce == Uint(2**64 - 1)
        or evm.message.depth + 1 > STACK_DEPTH_LIMIT
    ):
        push(evm.stack, U256(0))
        evm.gas_left += create_message_gas
    elif account_has_code_or_nonce(evm.env.state, contract_address):
        increment_nonce(evm.env.state, evm.message.current_target)
        push(evm.stack, U256(0))
    else:
        call_data = memory_read_bytes(
            evm.memory, memory_start_position, memory_size
        )

        increment_nonce(evm.env.state, evm.message.current_target)

        child_message = Message(
            caller=evm.message.current_target,
            target=Bytes0(),
            gas=U256(create_message_gas),
            value=endowment,
            data=b"",
            code=call_data,
            current_target=contract_address,
            depth=evm.message.depth + 1,
            code_address=None,
            should_transfer_value=True,
            is_static=False,
        )
        child_evm = process_create_message(child_message, evm.env)
        evm.children.append(child_evm)
        if child_evm.has_erred:
            push(evm.stack, U256(0))
            evm.return_data = child_evm.output
        else:
            evm.logs += child_evm.logs
            push(
                evm.stack, U256.from_be_bytes(child_evm.message.current_target)
            )
        evm.gas_left += child_evm.gas_left
        child_evm.gas_left = U256(0)

    # PROGRAM COUNTER
    evm.pc += 1


def return_(evm: Evm) -> None:
    """
    Halts execution returning output data.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    memory_start_position = pop(evm.stack)
    memory_size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_start_position, memory_size)
    charge_gas(evm, GAS_ZERO)

    # OPERATION
    evm.output = memory_read_bytes(
        evm.memory, memory_start_position, memory_size
    )

    evm.running = False

    # PROGRAM COUNTER
    pass


def generic_call(
    evm: Evm,
    gas: Uint,
    value: U256,
    caller: Address,
    to: Address,
    code_address: Address,
    should_transfer_value: bool,
    is_staticcall: bool,
    memory_input_start_position: U256,
    memory_input_size: U256,
    memory_output_start_position: U256,
    memory_output_size: U256,
) -> None:
    """
    Perform the core logic of the `CALL*` family of opcodes.
    """
    from ...vm.interpreter import STACK_DEPTH_LIMIT, process_message

    evm.return_data = b""

    if evm.message.depth + 1 > STACK_DEPTH_LIMIT:
        evm.gas_left += gas
        push(evm.stack, U256(0))
        return

    call_data = memory_read_bytes(
        evm.memory, memory_input_start_position, memory_input_size
    )
    code = get_account(evm.env.state, code_address).code
    child_message = Message(
        caller=caller,
        target=to,
        gas=U256(gas),
        value=value,
        data=call_data,
        code=code,
        current_target=to,
        depth=evm.message.depth + 1,
        code_address=code_address,
        should_transfer_value=should_transfer_value,
        is_static=True if is_staticcall else evm.message.is_static,
    )
    child_evm = process_message(child_message, evm.env)
    evm.children.append(child_evm)

    if child_evm.has_erred:
        push(evm.stack, U256(0))
        if isinstance(child_evm.error, Revert):
            evm.return_data = child_evm.output
    else:
        evm.logs += child_evm.logs
        push(evm.stack, U256(1))
        evm.return_data = child_evm.output

    actual_output_size = min(memory_output_size, U256(len(child_evm.output)))
    memory_write(
        evm.memory,
        memory_output_start_position,
        child_evm.output[:actual_output_size],
    )
    evm.gas_left += child_evm.gas_left
    child_evm.gas_left = U256(0)


def call(evm: Evm) -> None:
    """
    Message-call into an account.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    gas = Uint(pop(evm.stack))
    to = to_address(pop(evm.stack))
    value = pop(evm.stack)
    memory_input_start_position = pop(evm.stack)
    memory_input_size = pop(evm.stack)
    memory_output_start_position = pop(evm.stack)
    memory_output_size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_input_start_position, memory_input_size)
    extend_memory(evm, memory_output_start_position, memory_output_size)
    create_gas_cost = (
        Uint(0)
        if value == 0 or is_account_alive(evm.env.state, to)
        else GAS_NEW_ACCOUNT
    )
    transfer_gas_cost = Uint(0) if value == 0 else GAS_CALL_VALUE
    call_gas_fee = calculate_call_gas_cost(
        gas, Uint(evm.gas_left), GAS_CALL + create_gas_cost + transfer_gas_cost
    )
    message_call_gas = calculate_message_call_gas_stipend(
        value,
        gas,
        Uint(evm.gas_left),
        GAS_CALL + create_gas_cost + transfer_gas_cost,
    )
    charge_gas(evm, call_gas_fee)

    # OPERATION
    ensure(not evm.message.is_static or value == U256(0), WriteInStaticContext)
    sender_balance = get_account(
        evm.env.state, evm.message.current_target
    ).balance
    if sender_balance < value:
        push(evm.stack, U256(0))
        evm.return_data = b""
        evm.gas_left += message_call_gas
    else:
        generic_call(
            evm,
            message_call_gas,
            value,
            evm.message.current_target,
            to,
            to,
            True,
            False,
            memory_input_start_position,
            memory_input_size,
            memory_output_start_position,
            memory_output_size,
        )

    # PROGRAM COUNTER
    evm.pc += 1


def callcode(evm: Evm) -> None:
    """
    Message-call into this account with alternative account’s code.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    gas = Uint(pop(evm.stack))
    code_address = to_address(pop(evm.stack))
    value = pop(evm.stack)
    memory_input_start_position = pop(evm.stack)
    memory_input_size = pop(evm.stack)
    memory_output_start_position = pop(evm.stack)
    memory_output_size = pop(evm.stack)

    # GAS
    to = evm.message.current_target

    extend_memory(evm, memory_input_start_position, memory_input_size)
    extend_memory(evm, memory_output_start_position, memory_output_size)
    transfer_gas_cost = Uint(0) if value == 0 else GAS_CALL_VALUE
    call_gas_fee = calculate_call_gas_cost(
        gas, Uint(evm.gas_left), GAS_CALL + transfer_gas_cost
    )
    message_call_gas = calculate_message_call_gas_stipend(
        value,
        gas,
        Uint(evm.gas_left),
        GAS_CALL + transfer_gas_cost,
    )
    charge_gas(evm, call_gas_fee)

    # OPERATION
    sender_balance = get_account(
        evm.env.state, evm.message.current_target
    ).balance
    if sender_balance < value:
        push(evm.stack, U256(0))
        evm.return_data = b""
        evm.gas_left += message_call_gas
    else:
        generic_call(
            evm,
            message_call_gas,
            value,
            evm.message.current_target,
            to,
            code_address,
            True,
            False,
            memory_input_start_position,
            memory_input_size,
            memory_output_start_position,
            memory_output_size,
        )

    # PROGRAM COUNTER
    evm.pc += 1


def selfdestruct(evm: Evm) -> None:
    """
    Halt execution and register account for later deletion.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    beneficiary = to_address(pop(evm.stack))

    # GAS
    if (
        not is_account_alive(evm.env.state, beneficiary)
        and get_account(evm.env.state, evm.message.current_target).balance != 0
    ):
        charge_gas(evm, GAS_SELF_DESTRUCT + GAS_SELF_DESTRUCT_NEW_ACCOUNT)
    else:
        charge_gas(evm, GAS_SELF_DESTRUCT)

    # OPERATION
    ensure(not evm.message.is_static, WriteInStaticContext)

    originator = evm.message.current_target
    beneficiary_balance = get_account(evm.env.state, beneficiary).balance
    originator_balance = get_account(evm.env.state, originator).balance

    # First Transfer to beneficiary
    set_account_balance(
        evm.env.state, beneficiary, beneficiary_balance + originator_balance
    )
    # Next, Zero the balance of the address being deleted (must come after
    # sending to beneficiary in case the contract named itself as the
    # beneficiary).
    set_account_balance(evm.env.state, originator, U256(0))

    # register account for deletion
    evm.accounts_to_delete[originator] = beneficiary

    # HALT the execution
    evm.running = False

    # PROGRAM COUNTER
    pass


def delegatecall(evm: Evm) -> None:
    """
    Message-call into an account.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    gas = Uint(pop(evm.stack))
    code_address = to_address(pop(evm.stack))
    memory_input_start_position = pop(evm.stack)
    memory_input_size = pop(evm.stack)
    memory_output_start_position = pop(evm.stack)
    memory_output_size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_input_start_position, memory_input_size)
    extend_memory(evm, memory_output_start_position, memory_output_size)
    call_gas_fee = calculate_call_gas_cost(gas, Uint(evm.gas_left), GAS_CALL)
    message_call_gas = calculate_message_call_gas_stipend(
        U256(0), gas, Uint(evm.gas_left), GAS_CALL
    )
    charge_gas(evm, call_gas_fee)

    # OPERATION
    generic_call(
        evm,
        message_call_gas,
        evm.message.value,
        evm.message.caller,
        evm.message.current_target,
        code_address,
        False,
        False,
        memory_input_start_position,
        memory_input_size,
        memory_output_start_position,
        memory_output_size,
    )

    # PROGRAM COUNTER
    evm.pc += 1


def staticcall(evm: Evm) -> None:
    """
    Message-call into an account.

    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    gas = Uint(pop(evm.stack))
    to = to_address(pop(evm.stack))
    memory_input_start_position = pop(evm.stack)
    memory_input_size = pop(evm.stack)
    memory_output_start_position = pop(evm.stack)
    memory_output_size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_input_start_position, memory_input_size)
    extend_memory(evm, memory_output_start_position, memory_output_size)
    call_gas_fee = calculate_call_gas_cost(gas, Uint(evm.gas_left), GAS_CALL)
    message_call_gas = calculate_message_call_gas_stipend(
        U256(0),
        gas,
        Uint(evm.gas_left),
        GAS_CALL,
    )
    charge_gas(evm, call_gas_fee)

    # OPERATION
    generic_call(
        evm,
        message_call_gas,
        U256(0),
        evm.message.current_target,
        to,
        to,
        True,
        True,
        memory_input_start_position,
        memory_input_size,
        memory_output_start_position,
        memory_output_size,
    )

    # PROGRAM COUNTER
    evm.pc += 1


def revert(evm: Evm) -> None:
    """
    Stop execution and revert state changes, without consuming all provided gas
    and also has the ability to return a reason
    Parameters
    ----------
    evm :
        The current EVM frame.
    """
    # STACK
    memory_start_index = pop(evm.stack)
    size = pop(evm.stack)

    # GAS
    extend_memory(evm, memory_start_index, size)

    # OPERATION
    output = memory_read_bytes(evm.memory, memory_start_index, size)
    evm.output = bytes(output)
    raise Revert

    # PROGRAM COUNTER
    pass