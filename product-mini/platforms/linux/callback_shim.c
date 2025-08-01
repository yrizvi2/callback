#include "callback_shim.h"
#include <stdio.h>

bool call_callback_by_name(wasm_module_inst_t module_inst, uint32_t stack_size, uint32_t arg) {
    wasm_exec_env_t exec_env = wasm_runtime_create_exec_env(module_inst, stack_size);
    if (!exec_env) {
        printf("Failed to create exec env\n");
        return false;
    }

    wasm_function_inst_t addr_func = wasm_runtime_lookup_function(module_inst, "addr");
    if (!addr_func) {
        printf("Failed to find addr.\n");
        wasm_runtime_destroy_exec_env(exec_env);
        return false;
    }

    uint32_t results[1] = {0};
    if (!wasm_runtime_call_wasm(exec_env, addr_func, 0, results)) {
        printf("Failed to invoke 'addr': %s\n", wasm_runtime_get_exception(module_inst));
        wasm_runtime_destroy_exec_env(exec_env);
        return false;
    }

    uint32_t func_index = results[0];
    printf("Got function index from Wasm: %u\n", func_index);

    uint32_t argv[1] = { arg };
    if (!wasm_runtime_call_indirect(exec_env, func_index, 1, argv)) {
        printf("Indirect call failed: %s\n", wasm_runtime_get_exception(module_inst));
        wasm_runtime_destroy_exec_env(exec_env);
        return false;
    }

    printf("indirect call succeeded.\n");
    wasm_runtime_destroy_exec_env(exec_env);
    return true;
}