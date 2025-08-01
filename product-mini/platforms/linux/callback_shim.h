#ifndef CALLBACK_SHIM_H
#define CALLBACK_SHIM_H

#include <stdint.h> 
#include <stdbool.h>
#include "wasm_export.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * High-level wrapper:
 * Looks up a Wasm function by name, retrieves the table index from its return value,
 * and performs the indirect call with the given argument.
 */
bool call_callback_by_name(wasm_module_inst_t module_inst, uint32_t stack_size, uint32_t arg);

/**
 * Low-level version:
 * Performs an indirect call using a known table index and a single uint32_t argument.
 */
//bool call_callback(wasm_exec_env_t exec_env, uint32_t table_index, uint32_t arg);

/**
 * Extended version of call_callback_by_name:
 * Same behavior, but returns the created exec_env to the caller for manual cleanup.
 */
/** bool call_callback_by_name_with_env(wasm_module_inst_t module_inst,
                                    const char *exported_name,
                                    uint32_t arg,
                                    wasm_exec_env_t *out_exec_env);

**/

#ifdef __cplusplus
}
#endif

#endif // CALLBACK_SHIM_H