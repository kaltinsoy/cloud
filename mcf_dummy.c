#include <stdio.h>
#include <stdlib.h>

int main(int argc, char *argv[]) {
    // 256 MB'lık bir dizi ayır
    size_t size = (256 * 1024 * 1024) / sizeof(int);
    volatile int *data = (int *)malloc(size * sizeof(int));
    if (!data) return 1;
    
    // Rastgele atlamalar ile Donanım Prefetcher'ı yen (Yüksek Cache Miss üretir)
    for (size_t i = 0; i < size; i++) {
        data[i] = i;
    }
    
    // Sürekli bellek döngüsü
    for (size_t i = 0; i < size * 5; i++) {
        // 1009 asal sayısıyla atlama yap
        data[(i * 1009) % size] += 1;
    }
    
    free((void*)data);
    return 0;
}
