#include <stdint.h>
#include <stdio.h>

__attribute__((used)) __attribute__((export_name("print_num")))
void print_num(uint32_t x) {
    printf("you gave me = %d\n", x);
}

// Force print_num into the table
__attribute__((used))
void (*force_table_entry)(uint32_t) = print_num;

__attribute__((export_name("addr")))
uint32_t addr(void) {
    return 1; 
}
