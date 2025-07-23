#include <stdint.h>

typedef void (*Tfunc)(uint32_t);

void print_num(uint32_t x) {
}

Tfunc function = print_num;

#ifdef __wasm__
#define WASM_EXPORT(NAME) __attribute__((export_name(NAME)))
#else
#define WASM_EXPORT(NAME)
#endif

WASM_EXPORT("addr")
uint32_t addr() {
    return (uint32_t)(uintptr_t)&function;
}