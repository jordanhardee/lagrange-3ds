/* Nintendo 3DS newlib thread-specific-data backend.
 * The installed newlib exposes pthread_key_* through weak __syscall_tls_*
 * hooks which libctru 2.7.0 does not supply. Keep the public pthread API
 * and OpenSSL threading intact. Remove this shim once libctru implements
 * these hooks (strong duplicate symbols intentionally fail the build).
 */
#include <3ds/types.h>
#include <3ds/synchronization.h>
#include <3ds/thread.h>
#include <pthread.h>
#include <errno.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>

#define KEY_COUNT 64u
#define INDEX_MASK (KEY_COUNT - 1u)
#define MAX_GENERATION (UINT32_MAX / KEY_COUNT)
#define DESTRUCTOR_PASSES 4
_Static_assert(sizeof(pthread_key_t) == sizeof(uint32_t), "unexpected newlib key ABI");

typedef struct {
    uint32_t generation;
    bool live;
    void (*destroy)(void *);
} Key;
typedef struct {
    uint32_t generation;
    void *value;
} Value;

static LightLock key_lock = 1;
static Key keys[KEY_COUNT];
static _Thread_local Value values[KEY_COUNT];

static bool valid_locked(pthread_key_t key)
{
    const unsigned i = (uint32_t)key & INDEX_MASK;
    return keys[i].live && keys[i].generation == (uint32_t)key / KEY_COUNT;
}

int __syscall_tls_create(pthread_key_t *out, void (*destroy)(void *))
{
    if (!out) return EINVAL;
    LightLock_Lock(&key_lock);
    for (unsigned i = 0; i < KEY_COUNT; ++i) {
        if (!keys[i].live && keys[i].generation < MAX_GENERATION) {
            ++keys[i].generation;
            keys[i].live = true;
            keys[i].destroy = destroy;
            *out = (pthread_key_t)(keys[i].generation * KEY_COUNT + i);
            LightLock_Unlock(&key_lock);
            return 0;
        }
    }
    LightLock_Unlock(&key_lock);
    return EAGAIN;
}

int __syscall_tls_set(pthread_key_t key, const void *value)
{
    const unsigned i = (uint32_t)key & INDEX_MASK;
    LightLock_Lock(&key_lock);
    if (!valid_locked(key)) {
        LightLock_Unlock(&key_lock);
        return EINVAL;
    }
    values[i].generation = keys[i].generation;
    values[i].value = (void *)value;
    LightLock_Unlock(&key_lock);
    return 0;
}

void *__syscall_tls_get(pthread_key_t key)
{
    const unsigned i = (uint32_t)key & INDEX_MASK;
    void *value = NULL;
    LightLock_Lock(&key_lock);
    if (valid_locked(key) && values[i].generation == keys[i].generation)
        value = values[i].value;
    LightLock_Unlock(&key_lock);
    return value;
}

int __syscall_tls_delete(pthread_key_t key)
{
    const unsigned i = (uint32_t)key & INDEX_MASK;
    LightLock_Lock(&key_lock);
    if (!valid_locked(key)) {
        LightLock_Unlock(&key_lock);
        return EINVAL;
    }
    /* POSIX key deletion does not call destructors. Generation tags
       prevent an old value in any other thread from being reused. */
    keys[i].live = false;
    keys[i].destroy = NULL;
    values[i] = (Value){0};
    LightLock_Unlock(&key_lock);
    return 0;
}

static void cleanup_values(void)
{
    for (unsigned pass = 0; pass < DESTRUCTOR_PASSES; ++pass) {
        bool called = false;
        for (unsigned i = 0; i < KEY_COUNT; ++i) {
            void (*destroy)(void *) = NULL;
            void *value = NULL;
            LightLock_Lock(&key_lock);
            if (keys[i].live && values[i].generation == keys[i].generation &&
                values[i].value && keys[i].destroy) {
                destroy = keys[i].destroy;
                value = values[i].value;
                /* Destructors may call pthread_setspecific again. */
                values[i].value = NULL;
            }
            LightLock_Unlock(&key_lock);
            if (destroy) {
                destroy(value);
                called = true;
            }
        }
        if (!called) break;
    }
    for (unsigned i = 0; i < KEY_COUNT; ++i) values[i] = (Value){0};
}

typedef struct {
    ThreadFunc entry;
    void *arg;
} Start;

static void run_thread(void *arg)
{
    Start start = *(Start *)arg;
    free(arg);
    start.entry(start.arg);
    cleanup_values();
}

extern Thread __real_threadCreate(ThreadFunc, void *, size_t, int, int, bool);
extern void __real_threadExit(int) __attribute__((noreturn));

Thread __wrap_threadCreate(ThreadFunc entry, void *arg, size_t stack,
                           int priority, int core, bool detached)
{
    Start *start = malloc(sizeof(*start));
    if (!start) return NULL;
    *start = (Start){entry, arg};
    Thread thread = __real_threadCreate(run_thread, start, stack, priority, core, detached);
    if (!thread) free(start);
    return thread;
}

void __wrap_threadExit(int code)
{
    cleanup_values();
    __real_threadExit(code);
}

/* Exercise the public newlib API, not just the private syscall hooks.
 * A native libctru worker must get independent values and run destructors
 * before its stack/TLS block is reclaimed. Test key deletion/reuse as well.
 */
typedef struct {
    pthread_key_t key;
    int worker_value;
    int destructor_calls;
    bool isolated;
    bool set_ok;
} Probe;

static void probe_destructor(void *arg)
{
    Probe *p = arg;
    ++p->destructor_calls;
}

static void probe_worker(void *arg)
{
    Probe *p = arg;
    p->isolated = pthread_getspecific(p->key) == NULL;
    p->set_ok = pthread_setspecific(p->key, p) == 0 &&
                pthread_getspecific(p->key) == p;
}

int lagrange_3ds_tsd_selftest(void)
{
    Probe p = {0};
    int main_value = 1;
    pthread_key_t reused;
    if (pthread_key_create(&p.key, probe_destructor) != 0) return 1;
    if (pthread_setspecific(p.key, &main_value) != 0) return 2;
    Thread worker = threadCreate(probe_worker, &p, 32 * 1024, 0x3f, 0, false);
    if (!worker) return 3;
    threadJoin(worker, UINT64_MAX);
    threadFree(worker);
    if (!p.isolated || !p.set_ok || p.destructor_calls != 1 ||
        pthread_getspecific(p.key) != &main_value) return 4;
    if (pthread_key_delete(p.key) != 0) return 5;
    if (pthread_key_create(&reused, NULL) != 0) return 6;
    if (pthread_getspecific(reused) != NULL) return 7;
    if (pthread_key_delete(reused) != 0) return 8;
    return 0;
}
