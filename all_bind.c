/*
 * all_bind - bind anything on any input device.
 *
 * Grabs the configured devices and re-emits their input, applying the
 * active profile on the way:
 *
 *   - movement: invert / swap X-Y, per-axis sensitivity, wheel inversion
 *   - bindings: any key, button, wheel direction or d-pad direction can be
 *       disabled, remapped (to a key, button, wheel or combo),
 *       turbo'd (N presses per second while held / toggled),
 *       turned into a macro, run a shell command,
 *       shift to another profile while held, or switch profile.
 *
 * Output:
 *   - Keyboards and mice share one virtual device ("all_bind virtual
 *     device"), so modifiers from one device apply to keys from another.
 *     Remaps and macros are emitted there too.
 *   - Devices with absolute axes (controllers, touchpads, tablets) get a
 *     clone with identical capabilities ("<name> (all_bind)") so sticks,
 *     triggers and touch keep working.
 *   - Keyboard LEDs (caps lock ...) are forwarded back to the real keyboards.
 *
 * The config is ~/.config/all_bind/all_bind.conf, written by the GUI / CLI
 * (all_bind_gui.py); see parse_config() for the format. The engine watches
 * that folder and reloads by itself when the file is saved.
 *
 * Talks JSON lines on stdout (logs, status, and physical input events with
 * --events) and reads commands on stdin: "reload", "next", "prev",
 * "profile <index>", "status", "quit". SIGHUP reloads, SIGUSR1 / SIGUSR2
 * switch to the next / previous profile (with a notification),
 * SIGINT/SIGTERM exit and release the devices. Starting it stops any other
 * running all_bind.
 *
 * Usage: see usage() -  all_bind [--user NAME] [--events] [CONFIG],
 * all_bind next | prev | reload,  all_bind --cli ...,  all_bind --gui.
 * As a root system service (--user NAME), programs it starts run as NAME.
 *
 * Build:
 *   make            (or: gcc -O2 -Wall -Wextra -pthread all_bind.c -o all_bind)
 */

#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <pwd.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/inotify.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/signalfd.h>
#include <sys/stat.h>
#include <linux/input.h>
#include <linux/uinput.h>

#ifndef REL_WHEEL_HI_RES
#define REL_WHEEL_HI_RES 0x0b
#endif
#ifndef REL_HWHEEL_HI_RES
#define REL_HWHEEL_HI_RES 0x0c
#endif

#define ENGINE_NAME   "all_bind"
#define VIRTUAL_NAME  "all_bind virtual device"
#define CLONE_SUFFIX  " (all_bind)"
#define MAX_DEVICES   32
#define MAX_SPECS     64
#define MAX_LAYERS    16
#define MAX_OUTS      16
#define NAME_LEN      256

/* Pseudo codes for wheel and d-pad directions (triggers; wheel also output). */
#define W_UP     (-1)
#define W_DOWN   (-2)
#define W_RIGHT  (-3)
#define W_LEFT   (-4)
#define D_UP     (-5)
#define D_DOWN   (-6)
#define D_LEFT   (-7)
#define D_RIGHT  (-8)

#define NSEC 1000000000LL
#define BITS_LEN(max) ((max) / (8 * sizeof(long)) + 1)


/* ---------------------------------------------------------
 * Types
 * --------------------------------------------------------- */

enum act_type { A_DISABLE, A_REMAP, A_TURBO, A_MACRO, A_COMMAND, A_LAYER, A_SWITCH };
enum act_mode { M_HOLD, M_TOGGLE, M_ONCE, M_REPEAT, M_WHILE_HELD };
enum step_type { S_TAP, S_DOWN, S_UP, S_DELAY, S_MOVE, S_WHEEL, S_CMD };

struct step {
    int type;
    long long a, b, c;      /* meaning depends on type, times in ns */
    char *str;
};

struct action {
    int type;
    int mode;

    int outs[MAX_OUTS];
    int n_outs;

    double cps;
    long long press_ns;
    double jitter;          /* 0..0.95 */
    int burst;              /* turbo: stop after N presses, 0 = never */
    int count;              /* macro repeat count */
    long long loop_gap_ns;
    int profile;            /* layer / switch target */
    char *cmd;

    struct step *steps;
    int n_steps, cap_steps;

    /* runtime */
    pthread_mutex_t mtx;
    pthread_cond_t cond;
    int sync_ready;
    atomic_int stop;
    atomic_int running;
    int has_thread;
    pthread_t thread;
    unsigned char held[KEY_CNT];
    int layer_on;
    uint64_t rng;
};

struct binding {
    int trigger;
    char *devfilter;        /* NULL = any device */
    struct action act;
};

struct movement {
    int invert_x, invert_y, swap_xy;
    double sens_x, sens_y;
    int invert_wheel, invert_hwheel;
};

struct profile {
    char name[NAME_LEN];
    struct movement mv;
    struct binding *b;
    int n, cap;
};

struct spec {
    char name[NAME_LEN];
    int vendor, product;
};

enum { HELD_NONE, HELD_PASS, HELD_ACT };

struct dev {
    int fd;
    char path[64];
    char name[NAME_LEN];
    int hires_w, hires_hw;
    int has_led;

    /* Passthrough route: own clone (abs devices) or the shared device. */
    int clone_fd;
    unsigned char clone_down[KEY_CNT];

    unsigned char held_kind[KEY_CNT];
    struct action *held_act[KEY_CNT];

    /* d-pad (ABS_HAT0X / ABS_HAT0Y) */
    int hat[2];
    int hat_fwd[2];
    int hat_kind[4];            /* index: -(code) - 5 */
    struct action *hat_act[4];
};

struct failed {
    char path[64];
    char name[NAME_LEN];
};


/* ---------------------------------------------------------
 * Globals
 * --------------------------------------------------------- */

static char config_dir[PATH_MAX];
static char config_path[PATH_MAX];
static int config_loaded;
static int send_events;         /* --events: report physical input (for the GUI) */

/* Root service started with --user NAME: run programs as that user. */
static uid_t run_uid = (uid_t)-1;
static gid_t run_gid;
static gid_t run_groups[64];
static int run_ngroups;
static char run_user[64];
static char run_home[PATH_MAX];

static struct spec specs[MAX_SPECS];
static int n_specs;

static struct profile *profiles;
static int n_profiles;
static int base_profile;
static int layers[MAX_LAYERS];
static int n_layers;

static struct dev *devs[MAX_DEVICES];

static struct failed failed[MAX_DEVICES];
static int n_failed;

static int out_fd = -1;
static int out_hires_w, out_hires_hw;
static unsigned char out_down[KEY_CNT];
static pthread_mutex_t out_mtx = PTHREAD_MUTEX_INITIALIZER;

static double acc_x, acc_y;

static pthread_mutex_t print_mtx = PTHREAD_MUTEX_INITIALIZER;
static int stdout_ok = 1;

static int running = 1;
static time_t last_scan;


/* ---------------------------------------------------------
 * stdout protocol (JSON lines)
 * --------------------------------------------------------- */

static void json_str(FILE *f, const char *s)
{
    fputc('"', f);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\')
            fprintf(f, "\\%c", c);
        else if (c < 0x20)
            fprintf(f, "\\u%04x", c);
        else
            fputc(c, f);
    }
    fputc('"', f);
}

static void print_end(void)
{
    fputc('\n', stdout);
    if (fflush(stdout) != 0) {
        /* GUI went away; keep running silently. */
        stdout_ok = 0;
        int nul = open("/dev/null", O_WRONLY);
        if (nul >= 0) {
            dup2(nul, STDOUT_FILENO);
            dup2(nul, STDERR_FILENO);
            close(nul);
        }
        clearerr(stdout);
    }
}

static void logmsg(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    pthread_mutex_lock(&print_mtx);
    if (stdout_ok) {
        fputs("{\"t\":\"log\",\"msg\":", stdout);
        json_str(stdout, buf);
        fputc('}', stdout);
        print_end();
    }
    pthread_mutex_unlock(&print_mtx);
}

static void send_input(const struct dev *d, int code, int value)
{
    /* Off unless the GUI asks: keeps keystrokes out of logs and journals. */
    if (!send_events)
        return;

    pthread_mutex_lock(&print_mtx);
    if (stdout_ok) {
        fputs("{\"t\":\"input\",\"dev\":", stdout);
        json_str(stdout, d->name);
        printf(",\"code\":%d,\"value\":%d}", code, value);
        print_end();
    }
    pthread_mutex_unlock(&print_mtx);
}

static void send_status(void)
{
    pthread_mutex_lock(&print_mtx);
    if (stdout_ok) {
        int first = 1;
        fputs("{\"t\":\"status\",\"grabbed\":[", stdout);
        for (int i = 0; i < MAX_DEVICES; i++) {
            if (!devs[i])
                continue;
            if (!first)
                fputc(',', stdout);
            json_str(stdout, devs[i]->name);
            first = 0;
        }
        fputs("],\"failed\":[", stdout);
        for (int i = 0; i < n_failed; i++) {
            if (i)
                fputc(',', stdout);
            json_str(stdout, failed[i].name);
        }
        fputs("],\"profile\":", stdout);
        json_str(stdout, n_profiles ? profiles[base_profile].name : "");
        fputs(",\"layers\":[", stdout);
        for (int i = 0; i < n_layers; i++) {
            if (i)
                fputc(',', stdout);
            json_str(stdout, profiles[layers[i]].name);
        }
        fputs("]}", stdout);
        print_end();
    }
    pthread_mutex_unlock(&print_mtx);
}


/* ---------------------------------------------------------
 * Helpers
 * --------------------------------------------------------- */

static void ts_now(struct timespec *ts)
{
    clock_gettime(CLOCK_MONOTONIC, ts);
}

static void ts_add(struct timespec *ts, long long ns)
{
    long long t = ts->tv_nsec + ns;
    ts->tv_sec += t / NSEC;
    ts->tv_nsec = t % NSEC;
    if (ts->tv_nsec < 0) {
        ts->tv_nsec += NSEC;
        ts->tv_sec--;
    }
}

static long long ts_diff(const struct timespec *a, const struct timespec *b)
{
    return (a->tv_sec - b->tv_sec) * NSEC + (a->tv_nsec - b->tv_nsec);
}

static double rng_unit(struct action *a)
{
    /* xorshift64*, per action so threads don't share state */
    uint64_t x = a->rng;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    a->rng = x;
    return (double)((x * 2685821657736338717ULL) >> 11) / (double)(1ULL << 53);
}

static int test_bit(const unsigned long *bits, int bit)
{
    return (bits[bit / (8 * sizeof(long))] >> (bit % (8 * sizeof(long)))) & 1;
}

static int is_own_device(const char *name)
{
    size_t n = strlen(name), s = strlen(CLONE_SUFFIX);

    if (!strcmp(name, VIRTUAL_NAME))
        return 1;
    return n >= s && !strcmp(name + n - s, CLONE_SUFFIX);
}


/* ---------------------------------------------------------
 * Shared virtual device (keyboards, mice, remaps, macros)
 * --------------------------------------------------------- */

static void raw_write(int fd, int type, int code, int value)
{
    struct input_event ev;

    if (fd < 0)
        return;

    memset(&ev, 0, sizeof(ev));
    ev.type = type;
    ev.code = code;
    ev.value = value;

    /* O_NONBLOCK: drop on EAGAIN rather than stall a turbo thread. */
    if (write(fd, &ev, sizeof(ev)) != sizeof(ev)) {
    }
}

static void out_raw(int type, int code, int value)
{
    raw_write(out_fd, type, code, value);
}

static void out_syn(void)
{
    pthread_mutex_lock(&out_mtx);
    out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

static int out_key_allowed(int c)
{
    /*
     * Keyboard keys + mouse buttons only. Joystick / tablet button ranges
     * are left out so udev keeps classifying this as mouse + keyboard.
     */
    return (c >= 1 && c < 0x100)
        || (c >= BTN_LEFT && c <= BTN_TASK)
        || (c >= 0x160 && c < 0x220)
        || (c >= 0x230 && c < 0x2c0);
}

static void out_key(int code, int value, int syn)
{
    if (code < 0 || code >= KEY_CNT || !out_key_allowed(code))
        return;
    pthread_mutex_lock(&out_mtx);
    out_down[code] = value != 0;
    out_raw(EV_KEY, code, value);
    if (syn)
        out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

static void out_rel(int code, int value, int syn)
{
    pthread_mutex_lock(&out_mtx);
    out_raw(EV_REL, code, value);
    if (syn)
        out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

static void out_move(int dx, int dy)
{
    pthread_mutex_lock(&out_mtx);
    if (dx)
        out_raw(EV_REL, REL_X, dx);
    if (dy)
        out_raw(EV_REL, REL_Y, dy);
    out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

static void out_wheel(int horizontal, int notches)
{
    pthread_mutex_lock(&out_mtx);
    if (horizontal) {
        out_raw(EV_REL, REL_HWHEEL, notches);
        if (out_hires_hw)
            out_raw(EV_REL, REL_HWHEEL_HI_RES, notches * 120);
    } else {
        out_raw(EV_REL, REL_WHEEL, notches);
        if (out_hires_w)
            out_raw(EV_REL, REL_WHEEL_HI_RES, notches * 120);
    }
    out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

/* Press an output code: key/button, or a wheel pseudo code. */
static void out_press(int code)
{
    switch (code) {
    case W_UP:    out_wheel(0,  1); break;
    case W_DOWN:  out_wheel(0, -1); break;
    case W_RIGHT: out_wheel(1,  1); break;
    case W_LEFT:  out_wheel(1, -1); break;
    default:      out_key(code, 1, 1); break;
    }
}

static void out_release(int code)
{
    if (code >= 0)
        out_key(code, 0, 1);
}

static void out_release_all(void)
{
    pthread_mutex_lock(&out_mtx);
    for (int c = 0; c < KEY_CNT; c++) {
        if (out_down[c]) {
            out_raw(EV_KEY, c, 0);
            out_down[c] = 0;
        }
    }
    out_raw(EV_SYN, SYN_REPORT, 0);
    pthread_mutex_unlock(&out_mtx);
}

static void out_destroy_locked(void)
{
    if (out_fd < 0)
        return;
    for (int c = 0; c < KEY_CNT; c++) {
        if (out_down[c]) {
            out_raw(EV_KEY, c, 0);
            out_down[c] = 0;
        }
    }
    out_raw(EV_SYN, SYN_REPORT, 0);
    ioctl(out_fd, UI_DEV_DESTROY);
    close(out_fd);
    out_fd = -1;
}

static int out_create(int hires_w, int hires_hw)
{
    struct uinput_setup setup;
    int fd;

    /* O_RDWR: the compositor's LED changes come back on this fd. */
    fd = open("/dev/uinput", O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        logmsg("open /dev/uinput: %s", strerror(errno));
        return -1;
    }

    ioctl(fd, UI_SET_EVBIT, EV_SYN);
    ioctl(fd, UI_SET_EVBIT, EV_KEY);
    ioctl(fd, UI_SET_EVBIT, EV_REL);
    ioctl(fd, UI_SET_EVBIT, EV_LED);

    ioctl(fd, UI_SET_RELBIT, REL_X);
    ioctl(fd, UI_SET_RELBIT, REL_Y);
    ioctl(fd, UI_SET_RELBIT, REL_WHEEL);
    ioctl(fd, UI_SET_RELBIT, REL_HWHEEL);

    /*
     * libinput ignores REL_WHEEL once a device advertises hi-res scrolling,
     * so only advertise it when a grabbed device really sends it.
     */
    if (hires_w)
        ioctl(fd, UI_SET_RELBIT, REL_WHEEL_HI_RES);
    if (hires_hw)
        ioctl(fd, UI_SET_RELBIT, REL_HWHEEL_HI_RES);

    for (int c = 0; c < KEY_CNT; c++)
        if (out_key_allowed(c))
            ioctl(fd, UI_SET_KEYBIT, c);

    ioctl(fd, UI_SET_LEDBIT, LED_NUML);
    ioctl(fd, UI_SET_LEDBIT, LED_CAPSL);
    ioctl(fd, UI_SET_LEDBIT, LED_SCROLLL);
    ioctl(fd, UI_SET_LEDBIT, LED_COMPOSE);
    ioctl(fd, UI_SET_LEDBIT, LED_KANA);

    memset(&setup, 0, sizeof(setup));
    snprintf(setup.name, sizeof(setup.name), VIRTUAL_NAME);
    setup.id.bustype = BUS_USB;
    setup.id.vendor = 0x1234;
    setup.id.product = 0x5679;
    setup.id.version = 1;

    if (ioctl(fd, UI_DEV_SETUP, &setup) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
        logmsg("creating virtual device failed: %s", strerror(errno));
        close(fd);
        return -1;
    }

    pthread_mutex_lock(&out_mtx);
    out_destroy_locked();
    out_fd = fd;
    out_hires_w = hires_w;
    out_hires_hw = hires_hw;
    pthread_mutex_unlock(&out_mtx);
    return 0;
}

/* (Re)create the shared device if the hi-res wheel capabilities changed. */
static void out_sync_caps(void)
{
    int hw = 0, hh = 0;

    for (int i = 0; i < MAX_DEVICES; i++) {
        if (devs[i] && devs[i]->clone_fd < 0) {
            hw |= devs[i]->hires_w;
            hh |= devs[i]->hires_hw;
        }
    }
    if (out_fd >= 0 && hw == out_hires_w && hh == out_hires_hw)
        return;
    out_create(hw, hh);
}


/* ---------------------------------------------------------
 * Per-device clones (controllers, touchpads, tablets)
 * --------------------------------------------------------- */

/* Devices with real absolute axes need a clone to keep them working. */
static int needs_clone(int fd)
{
    unsigned long absbits[BITS_LEN(ABS_MAX)] = { 0 };
    unsigned long keybits[BITS_LEN(KEY_MAX)] = { 0 };

    ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(absbits)), absbits);
    ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits);

    for (int a = ABS_X; a <= ABS_RZ; a++)
        if (test_bit(absbits, a))
            return 1;
    for (int a = ABS_HAT0X; a <= ABS_HAT3Y; a++)
        if (test_bit(absbits, a))
            return 1;
    if (test_bit(absbits, ABS_MT_SLOT) || test_bit(absbits, ABS_MT_POSITION_X))
        return 1;
    return test_bit(keybits, BTN_SOUTH) || test_bit(keybits, BTN_TRIGGER)
        || test_bit(keybits, BTN_TOUCH) || test_bit(keybits, BTN_TOOL_PEN);
}

static int clone_create(int src, const char *name)
{
    unsigned long evbits[BITS_LEN(EV_MAX)] = { 0 };
    unsigned long keybits[BITS_LEN(KEY_MAX)] = { 0 };
    unsigned long relbits[BITS_LEN(REL_MAX)] = { 0 };
    unsigned long absbits[BITS_LEN(ABS_MAX)] = { 0 };
    unsigned long mscbits[BITS_LEN(MSC_MAX)] = { 0 };
    unsigned long ledbits[BITS_LEN(LED_MAX)] = { 0 };
    unsigned long propbits[BITS_LEN(INPUT_PROP_MAX)] = { 0 };
    struct uinput_setup setup;
    struct input_id id;
    int fd;

    fd = open("/dev/uinput", O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        logmsg("open /dev/uinput: %s", strerror(errno));
        return -1;
    }

    ioctl(src, EVIOCGBIT(0, sizeof(evbits)), evbits);
    ioctl(src, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits);
    ioctl(src, EVIOCGBIT(EV_REL, sizeof(relbits)), relbits);
    ioctl(src, EVIOCGBIT(EV_ABS, sizeof(absbits)), absbits);
    ioctl(src, EVIOCGBIT(EV_MSC, sizeof(mscbits)), mscbits);
    ioctl(src, EVIOCGBIT(EV_LED, sizeof(ledbits)), ledbits);
    ioctl(src, EVIOCGPROP(sizeof(propbits)), propbits);

    ioctl(fd, UI_SET_EVBIT, EV_SYN);
    if (test_bit(evbits, EV_KEY)) {
        ioctl(fd, UI_SET_EVBIT, EV_KEY);
        for (int c = 0; c < KEY_CNT; c++)
            if (test_bit(keybits, c))
                ioctl(fd, UI_SET_KEYBIT, c);
    }
    if (test_bit(evbits, EV_REL)) {
        ioctl(fd, UI_SET_EVBIT, EV_REL);
        for (int c = 0; c < REL_CNT; c++)
            if (test_bit(relbits, c))
                ioctl(fd, UI_SET_RELBIT, c);
    }
    if (test_bit(evbits, EV_MSC)) {
        ioctl(fd, UI_SET_EVBIT, EV_MSC);
        for (int c = 0; c < MSC_CNT; c++)
            if (test_bit(mscbits, c))
                ioctl(fd, UI_SET_MSCBIT, c);
    }
    if (test_bit(evbits, EV_LED)) {
        ioctl(fd, UI_SET_EVBIT, EV_LED);
        for (int c = 0; c < LED_CNT; c++)
            if (test_bit(ledbits, c))
                ioctl(fd, UI_SET_LEDBIT, c);
    }
    for (int c = 0; c < INPUT_PROP_CNT; c++)
        if (test_bit(propbits, c))
            ioctl(fd, UI_SET_PROPBIT, c);

    if (test_bit(evbits, EV_ABS)) {
        ioctl(fd, UI_SET_EVBIT, EV_ABS);
        for (int c = 0; c < ABS_CNT; c++) {
            struct uinput_abs_setup abs;

            if (!test_bit(absbits, c))
                continue;
            memset(&abs, 0, sizeof(abs));
            abs.code = c;
            if (ioctl(src, EVIOCGABS(c), &abs.absinfo) < 0)
                continue;
            ioctl(fd, UI_SET_ABSBIT, c);
            ioctl(fd, UI_ABS_SETUP, &abs);
        }
    }

    memset(&setup, 0, sizeof(setup));
    snprintf(setup.name, sizeof(setup.name), "%.*s%s",
             (int)(sizeof(setup.name) - sizeof(CLONE_SUFFIX)), name, CLONE_SUFFIX);
    if (ioctl(src, EVIOCGID, &id) == 0)
        setup.id = id;      /* same ids so games recognise the controller */

    if (ioctl(fd, UI_DEV_SETUP, &setup) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
        logmsg("cloning %s failed: %s", name, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}


/* ---------------------------------------------------------
 * Passthrough (to the device's route)
 * --------------------------------------------------------- */

static void pass_raw(struct dev *d, int type, int code, int value)
{
    pthread_mutex_lock(&out_mtx);
    raw_write(d->clone_fd >= 0 ? d->clone_fd : out_fd, type, code, value);
    pthread_mutex_unlock(&out_mtx);
}

static void pass_key(struct dev *d, int code, int value)
{
    if (d->clone_fd >= 0) {
        d->clone_down[code] = value != 0;
        pass_raw(d, EV_KEY, code, value);
    } else {
        out_key(code, value, 0);
    }
}

static int pass_is_down(struct dev *d, int code)
{
    return d->clone_fd >= 0 ? d->clone_down[code] : out_down[code];
}

static void pass_rel(struct dev *d, int code, int value)
{
    if (d->clone_fd >= 0)
        pass_raw(d, EV_REL, code, value);
    else
        out_rel(code, value, 0);
}


/* ---------------------------------------------------------
 * Actions
 * --------------------------------------------------------- */

static int starts_with(const char *s, const char *prefix)
{
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

/*
 * Environment for programs run on behalf of the user when the engine is a
 * root service: their home, session bus (notifications), Wayland display
 * and Hyprland instance (hyprctl). Built before fork(); freed by the caller.
 */
static char **user_env(void)
{
    extern char **environ;
    static const char *const drop[] = {
        "HOME=", "USER=", "LOGNAME=", "MAIL=", "SHELL=", "XDG_RUNTIME_DIR=",
        "DBUS_SESSION_BUS_ADDRESS=", "WAYLAND_DISPLAY=", "HYPRLAND_INSTANCE_SIGNATURE=", NULL
    };
    char rt[64], best[256] = "", *s;
    time_t best_time = 0;
    struct dirent *de;
    DIR *dir;
    int n = 0, k = 0;
    char **env;

    snprintf(rt, sizeof(rt), "/run/user/%u", (unsigned)run_uid);
    for (char **e = environ; *e; e++)
        n++;
    env = calloc(n + 16, sizeof(*env));
    if (!env)
        return NULL;

    for (char **e = environ; *e; e++) {
        int skip = 0;
        for (int i = 0; drop[i]; i++)
            skip |= starts_with(*e, drop[i]);
        if (!skip)
            env[k++] = strdup(*e);
    }

    if (asprintf(&s, "HOME=%s", run_home) > 0) env[k++] = s;
    if (asprintf(&s, "USER=%s", run_user) > 0) env[k++] = s;
    if (asprintf(&s, "LOGNAME=%s", run_user) > 0) env[k++] = s;
    if (asprintf(&s, "XDG_RUNTIME_DIR=%s", rt) > 0) env[k++] = s;
    if (asprintf(&s, "DBUS_SESSION_BUS_ADDRESS=unix:path=%s/bus", rt) > 0) env[k++] = s;

    if ((dir = opendir(rt))) {
        while ((de = readdir(dir))) {
            if (starts_with(de->d_name, "wayland-") && !strstr(de->d_name, ".lock")) {
                if (asprintf(&s, "WAYLAND_DISPLAY=%s", de->d_name) > 0)
                    env[k++] = s;
                break;
            }
        }
        closedir(dir);
    }

    /* Newest Hyprland instance. */
    snprintf(best, sizeof(best), "%s/hypr", rt);
    if ((dir = opendir(best))) {
        char newest[256] = "";
        while ((de = readdir(dir))) {
            char p[512];
            struct stat st;
            if (de->d_name[0] == '.')
                continue;
            snprintf(p, sizeof(p), "%s/%s", best, de->d_name);
            if (stat(p, &st) == 0 && S_ISDIR(st.st_mode) && st.st_mtime >= best_time) {
                best_time = st.st_mtime;
                snprintf(newest, sizeof(newest), "%s", de->d_name);
            }
        }
        closedir(dir);
        if (newest[0] && asprintf(&s, "HYPRLAND_INSTANCE_SIGNATURE=%s", newest) > 0)
            env[k++] = s;
    }

    env[k] = NULL;
    return env;
}

static void free_env(char **env)
{
    if (!env)
        return;
    for (char **e = env; *e; e++)
        free(*e);
    free(env);
}

/* Run a program detached (argv[0] looked up in PATH), as the user if root. */
static void spawn_argv(char *const argv[])
{
    int as_user = run_uid != (uid_t)-1;
    char **env = as_user ? user_env() : NULL;
    pid_t pid;

    if (as_user && !env)
        return;

    pid = fork();
    if (pid < 0) {
        logmsg("fork: %s", strerror(errno));
        free_env(env);
        return;
    }
    if (pid == 0) {
        sigset_t all;
        int nul;

        sigemptyset(&all);
        sigprocmask(SIG_SETMASK, &all, NULL);
        signal(SIGPIPE, SIG_DFL);
        signal(SIGCHLD, SIG_DFL);
        setsid();

        nul = open("/dev/null", O_RDWR);
        if (nul >= 0) {
            dup2(nul, 0);
            dup2(nul, 1);
            dup2(nul, 2);
        }
        if (as_user) {
            /* Never run user-configured commands as root. */
            if (setgroups(run_ngroups, run_groups) < 0 || setgid(run_gid) < 0
                || setuid(run_uid) < 0 || getuid() == 0)
                _exit(126);
            if (chdir(run_home) < 0) {
            }
            execvpe(argv[0], argv, env);
        } else {
            execvp(argv[0], argv);
        }
        _exit(127);
    }
    /* SIGCHLD is SIG_IGN, so the child is reaped automatically. */
    free_env(env);
}

static void spawn_cmd(const char *cmd)
{
    char *argv[] = { "/bin/sh", "-c", (char *)cmd, NULL };

    if (cmd && *cmd)
        spawn_argv(argv);
}

/* Desktop notification; replaces the previous all_bind one. */
static void notify(const char *title, const char *body)
{
    char *argv[] = {
        "notify-send", "-a", ENGINE_NAME, "-t", "2000",
        "-h", "string:x-canonical-private-synchronous:" ENGINE_NAME,
        "-h", "string:x-dunst-stack-tag:" ENGINE_NAME,
        (char *)title, (char *)body, NULL
    };

    spawn_argv(argv);
}

/* Interruptible absolute sleep. Returns 1 if the action was stopped. */
static int act_wait_until(struct action *a, const struct timespec *deadline)
{
    int stopped;

    pthread_mutex_lock(&a->mtx);
    while (!atomic_load(&a->stop)) {
        if (pthread_cond_timedwait(&a->cond, &a->mtx, deadline) == ETIMEDOUT)
            break;
    }
    stopped = atomic_load(&a->stop);
    pthread_mutex_unlock(&a->mtx);
    return stopped;
}

static int act_sleep(struct action *a, long long ns)
{
    struct timespec t;

    if (ns <= 0)
        return atomic_load(&a->stop);
    ts_now(&t);
    ts_add(&t, ns);
    return act_wait_until(a, &t);
}

static void act_down(struct action *a, int code)
{
    out_press(code);
    if (code >= 0 && code < KEY_CNT)
        a->held[code] = 1;
}

static void act_up(struct action *a, int code)
{
    out_release(code);
    if (code >= 0 && code < KEY_CNT)
        a->held[code] = 0;
}

static void act_release_held(struct action *a)
{
    for (int c = 0; c < KEY_CNT; c++) {
        if (a->held[c]) {
            a->held[c] = 0;
            out_key(c, 0, 1);
        }
    }
}

static void run_turbo(struct action *a)
{
    long long period = (long long)(NSEC / (a->cps > 0.1 ? a->cps : 0.1));
    long long press = a->press_ns;
    struct timespec next, now, t;
    int count = 0;

    if (press > period * 9 / 10)
        press = period * 9 / 10;

    ts_now(&next);

    while (!atomic_load(&a->stop)) {
        for (int i = 0; i < a->n_outs; i++)
            act_down(a, a->outs[i]);

        if (press > 0) {
            ts_now(&t);
            ts_add(&t, press);
            if (act_wait_until(a, &t))
                break;
        }

        for (int i = 0; i < a->n_outs; i++)
            act_up(a, a->outs[i]);

        if (a->burst && ++count >= a->burst)
            break;

        if (a->jitter > 0)
            ts_add(&next, (long long)(period * (1.0 + a->jitter * (2.0 * rng_unit(a) - 1.0))));
        else
            ts_add(&next, period);

        /* Fell behind (system hiccup): resync instead of bursting. */
        ts_now(&now);
        if (ts_diff(&now, &next) > period)
            next = now;

        if (act_wait_until(a, &next))
            break;
    }
}

static int run_move(struct action *a, const struct step *s)
{
    int dx = (int)s->a, dy = (int)s->b;
    long long dur = s->c;
    struct timespec start, t;
    int n, sx = 0, sy = 0;

    if (dur <= 0) {
        out_move(dx, dy);
        return 0;
    }

    /* Smooth move in ~1 ms slices; rounding keeps the total exact. */
    n = (int)(dur / 1000000LL);
    if (n < 1)
        n = 1;
    ts_now(&start);
    for (int i = 1; i <= n; i++) {
        int tx = (int)((long long)dx * i / n);
        int ty = (int)((long long)dy * i / n);
        if (tx != sx || ty != sy)
            out_move(tx - sx, ty - sy);
        sx = tx;
        sy = ty;
        t = start;
        ts_add(&t, dur * i / n);
        if (act_wait_until(a, &t))
            return 1;
    }
    return 0;
}

/* Returns 1 if stopped part-way. */
static int run_steps(struct action *a)
{
    for (int i = 0; i < a->n_steps; i++) {
        const struct step *s = &a->steps[i];

        if (atomic_load(&a->stop))
            return 1;

        switch (s->type) {
        case S_TAP:
            act_down(a, (int)s->a);
            if (s->b > 0 && act_sleep(a, s->b))
                return 1;
            act_up(a, (int)s->a);
            break;
        case S_DOWN:
            act_down(a, (int)s->a);
            break;
        case S_UP:
            act_up(a, (int)s->a);
            break;
        case S_DELAY: {
            long long d = s->a;
            if (s->b > 0)
                d += (long long)(s->b * rng_unit(a));
            if (act_sleep(a, d))
                return 1;
            break;
        }
        case S_MOVE:
            if (run_move(a, s))
                return 1;
            break;
        case S_WHEEL:
            if (s->a)
                out_wheel((int)s->b, (int)s->a);
            break;
        case S_CMD:
            spawn_cmd(s->str);
            break;
        }
    }
    return 0;
}

static void run_macro(struct action *a)
{
    int loops = a->mode == M_ONCE ? 1 : a->mode == M_REPEAT ? (a->count > 0 ? a->count : 1) : -1;

    for (int i = 0; loops < 0 || i < loops; i++) {
        if (run_steps(a))
            return;
        if (loops >= 0 && i + 1 >= loops)
            return;
        if (a->loop_gap_ns > 0) {
            if (act_sleep(a, a->loop_gap_ns))
                return;
        } else {
            if (atomic_load(&a->stop))
                return;
            sched_yield();   /* keep a zero-delay loop from hogging the lock */
        }
    }
}

static void *action_thread(void *arg)
{
    struct action *a = arg;

    if (a->type == A_TURBO)
        run_turbo(a);
    else
        run_macro(a);

    act_release_held(a);
    atomic_store(&a->running, 0);
    return NULL;
}

static void act_start(struct action *a)
{
    if (atomic_load(&a->running))
        return;
    if (a->has_thread) {
        pthread_join(a->thread, NULL);
        a->has_thread = 0;
    }
    atomic_store(&a->stop, 0);
    atomic_store(&a->running, 1);
    if (pthread_create(&a->thread, NULL, action_thread, a) != 0) {
        logmsg("pthread_create failed");
        atomic_store(&a->running, 0);
        return;
    }
    a->has_thread = 1;
}

static void act_stop(struct action *a)
{
    if (!a->sync_ready)
        return;
    pthread_mutex_lock(&a->mtx);
    atomic_store(&a->stop, 1);
    pthread_cond_broadcast(&a->cond);
    pthread_mutex_unlock(&a->mtx);
    if (a->has_thread) {
        pthread_join(a->thread, NULL);
        a->has_thread = 0;
    }
    act_release_held(a);
    a->layer_on = 0;
}

static void layers_apply(struct action *a)
{
    int p = a->profile, found = -1;

    if (p < 0 || p >= n_profiles)
        return;
    for (int i = 0; i < n_layers; i++)
        if (layers[i] == p)
            found = i;

    if (a->layer_on && found < 0 && n_layers < MAX_LAYERS) {
        layers[n_layers++] = p;
    } else if (!a->layer_on && found >= 0) {
        memmove(&layers[found], &layers[found + 1], (n_layers - found - 1) * sizeof(int));
        n_layers--;
    }
    send_status();
}

static void switch_profile(int p);

static void act_press(struct action *a)
{
    switch (a->type) {
    case A_DISABLE:
        break;
    case A_REMAP:
        for (int i = 0; i < a->n_outs; i++)
            out_press(a->outs[i]);
        break;
    case A_TURBO:
    case A_MACRO:
        if (a->mode == M_TOGGLE && atomic_load(&a->running))
            act_stop(a);
        else
            act_start(a);
        break;
    case A_COMMAND:
        spawn_cmd(a->cmd);
        break;
    case A_LAYER:
        a->layer_on = a->mode == M_TOGGLE ? !a->layer_on : 1;
        layers_apply(a);
        break;
    case A_SWITCH:
        switch_profile(a->profile);
        break;
    }
}

static void act_release(struct action *a)
{
    switch (a->type) {
    case A_REMAP:
        for (int i = a->n_outs - 1; i >= 0; i--)
            out_release(a->outs[i]);
        break;
    case A_TURBO:
        if (a->mode == M_HOLD)
            act_stop(a);
        break;
    case A_MACRO:
        if (a->mode == M_WHILE_HELD)
            act_stop(a);
        break;
    case A_LAYER:
        if (a->mode == M_HOLD) {
            a->layer_on = 0;
            layers_apply(a);
        }
        break;
    default:
        break;
    }
}


/* ---------------------------------------------------------
 * Profiles
 * --------------------------------------------------------- */

static void clear_held_routes(void)
{
    for (int i = 0; i < MAX_DEVICES; i++) {
        struct dev *d = devs[i];
        if (!d)
            continue;
        memset(d->held_kind, 0, sizeof(d->held_kind));
        memset(d->held_act, 0, sizeof(d->held_act));
        memset(d->hat_kind, 0, sizeof(d->hat_kind));
        memset(d->hat_act, 0, sizeof(d->hat_act));
    }
}

static void stop_all_actions(void)
{
    for (int p = 0; p < n_profiles; p++)
        for (int i = 0; i < profiles[p].n; i++)
            act_stop(&profiles[p].b[i].act);
    clear_held_routes();
    n_layers = 0;
}

static void switch_profile(int p)
{
    if (p < 0 || p >= n_profiles) {
        logmsg("no such profile: %d", p);
        return;
    }
    char body[NAME_LEN + 32];

    stop_all_actions();
    out_release_all();
    base_profile = p;
    logmsg("profile -> %s", profiles[p].name);
    send_status();

    snprintf(body, sizeof(body), "%s  (%d/%d)", profiles[p].name, p + 1, n_profiles);
    notify("Profile", body);
}

/* SIGUSR1 / SIGUSR2: next / previous profile (see all_bind_cycle). */
static void cycle_profile(int step)
{
    if (n_profiles > 0)
        switch_profile(((base_profile + step) % n_profiles + n_profiles) % n_profiles);
}

static const struct movement *active_movement(void)
{
    static const struct movement none = { 0, 0, 0, 1.0, 1.0, 0, 0 };

    if (n_layers)
        return &profiles[layers[n_layers - 1]].mv;
    if (n_profiles)
        return &profiles[base_profile].mv;
    return &none;
}

static struct binding *profile_find(struct profile *p, int trigger, const char *devname)
{
    struct binding *any = NULL;

    for (int i = 0; i < p->n; i++) {
        struct binding *b = &p->b[i];
        if (b->trigger != trigger)
            continue;
        if (b->devfilter) {
            if (strcmp(b->devfilter, devname) == 0)
                return b;   /* device-specific beats "any device" */
        } else if (!any) {
            any = b;
        }
    }
    return any;
}

/* Layers (top first) fall through to the base profile. */
static struct binding *lookup(int trigger, const char *devname)
{
    struct binding *b;

    for (int i = n_layers - 1; i >= 0; i--)
        if ((b = profile_find(&profiles[layers[i]], trigger, devname)))
            return b;
    if (n_profiles)
        return profile_find(&profiles[base_profile], trigger, devname);
    return NULL;
}


/* ---------------------------------------------------------
 * Config
 *
 * Line based, one directive per line, '#' comments:
 *
 *   device <vendor> <product> <name...>
 *   active <profile index>
 *   profile <name...>
 *   move <inv_x> <inv_y> <swap> <sens_x> <sens_y> <inv_wheel> <inv_hwheel>
 *   bind <trigger code> <disable|remap|turbo|macro|command|layer|switch>
 *     dev <device name...>             only from this device
 *     opt <key> <value>                mode cps press_ms jitter burst
 *                                      count loop_gap_ms profile
 *     out <code>                       output key (remap / turbo)
 *     cmd <shell command...>
 *     step tap <code> <hold_ms>
 *     step down <code>
 *     step up <code>
 *     step delay <ms> <random_ms>
 *     step move <dx> <dy> <duration_ms>
 *     step wheel <notches> <horizontal>
 *     step cmd <shell command...>
 *
 * Codes are linux/input-event-codes.h values; wheel directions are
 * -1 up, -2 down, -3 right, -4 left; d-pad -5 up, -6 down, -7 left,
 * -8 right.
 * --------------------------------------------------------- */

static void free_profiles(void)
{
    for (int p = 0; p < n_profiles; p++) {
        for (int i = 0; i < profiles[p].n; i++) {
            struct binding *b = &profiles[p].b[i];
            struct action *a = &b->act;
            if (a->sync_ready) {
                pthread_mutex_destroy(&a->mtx);
                pthread_cond_destroy(&a->cond);
            }
            for (int s = 0; s < a->n_steps; s++)
                free(a->steps[s].str);
            free(a->steps);
            free(a->cmd);
            free(b->devfilter);
        }
        free(profiles[p].b);
    }
    free(profiles);
    profiles = NULL;
    n_profiles = 0;
}

static char *skip_words(char *s, int n)
{
    while (n-- > 0) {
        while (*s == ' ' || *s == '\t')
            s++;
        while (*s && *s != ' ' && *s != '\t')
            s++;
    }
    while (*s == ' ' || *s == '\t')
        s++;
    return s;
}

static int parse_mode(const char *s)
{
    if (!strcmp(s, "toggle"))     return M_TOGGLE;
    if (!strcmp(s, "once"))       return M_ONCE;
    if (!strcmp(s, "repeat"))     return M_REPEAT;
    if (!strcmp(s, "while_held")) return M_WHILE_HELD;
    return M_HOLD;
}

static int parse_type(const char *s)
{
    if (!strcmp(s, "remap"))   return A_REMAP;
    if (!strcmp(s, "turbo"))   return A_TURBO;
    if (!strcmp(s, "macro"))   return A_MACRO;
    if (!strcmp(s, "command")) return A_COMMAND;
    if (!strcmp(s, "layer"))   return A_LAYER;
    if (!strcmp(s, "switch"))  return A_SWITCH;
    return A_DISABLE;
}

static long long ms_to_ns(const char *s)
{
    return s ? (long long)(strtod(s, NULL) * 1e6) : 0;
}

static struct step *add_step(struct action *a)
{
    if (a->n_steps == a->cap_steps) {
        a->cap_steps = a->cap_steps ? a->cap_steps * 2 : 8;
        a->steps = realloc(a->steps, a->cap_steps * sizeof(*a->steps));
    }
    memset(&a->steps[a->n_steps], 0, sizeof(struct step));
    return &a->steps[a->n_steps++];
}

static int parse_config(void)
{
    FILE *f = fopen(config_path, "r");
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    int lineno = 0, active = 0;
    struct profile *prof = NULL;
    struct binding *bind = NULL;
    static int warned_missing;
    struct stat st;

    if (!f) {
        if (!warned_missing)
            logmsg("cannot open %s: %s - waiting for it (open all_bind-gui once to create it)",
                   config_path, strerror(errno));
        warned_missing = 1;
        return -1;
    }
    warned_missing = 0;

    /*
     * As a root service the config decides what runs: only trust a file the
     * user (or root) owns and nobody else can write.
     */
    if (run_uid != (uid_t)-1 && fstat(fileno(f), &st) == 0
        && ((st.st_uid != run_uid && st.st_uid != 0) || (st.st_mode & 022))) {
        logmsg("refusing %s: must be owned by %s and not group/world writable",
               config_path, run_user);
        fclose(f);
        return -1;
    }

    free_profiles();
    n_specs = 0;

    while ((len = getline(&line, &cap, f)) >= 0) {
        char kw[32] = "", w1[64] = "", w2[64] = "", w3[64] = "";
        lineno++;

        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
            line[--len] = '\0';
        if (sscanf(line, "%31s %63s %63s %63s", kw, w1, w2, w3) < 1 || kw[0] == '#')
            continue;

        if (!strcmp(kw, "device")) {
            if (n_specs < MAX_SPECS) {
                struct spec *s = &specs[n_specs++];
                s->vendor = (int)strtol(w1, NULL, 0);
                s->product = (int)strtol(w2, NULL, 0);
                snprintf(s->name, sizeof(s->name), "%s", skip_words(line, 3));
            }
        } else if (!strcmp(kw, "active")) {
            active = atoi(w1);
        } else if (!strcmp(kw, "profile")) {
            profiles = realloc(profiles, (n_profiles + 1) * sizeof(*profiles));
            prof = &profiles[n_profiles++];
            memset(prof, 0, sizeof(*prof));
            prof->mv.sens_x = prof->mv.sens_y = 1.0;
            snprintf(prof->name, sizeof(prof->name), "%s", skip_words(line, 1));
            bind = NULL;
        } else if (!strcmp(kw, "move") && prof) {
            struct movement *m = &prof->mv;
            if (sscanf(line, "move %d %d %d %lf %lf %d %d", &m->invert_x, &m->invert_y,
                       &m->swap_xy, &m->sens_x, &m->sens_y, &m->invert_wheel,
                       &m->invert_hwheel) != 7)
                logmsg("config line %d: bad move", lineno);
        } else if (!strcmp(kw, "bind") && prof) {
            if (prof->n == prof->cap) {
                prof->cap = prof->cap ? prof->cap * 2 : 8;
                prof->b = realloc(prof->b, prof->cap * sizeof(*prof->b));
            }
            bind = &prof->b[prof->n++];
            memset(bind, 0, sizeof(*bind));
            bind->trigger = atoi(w1);
            bind->act.type = parse_type(w2);
            bind->act.mode = bind->act.type == A_MACRO ? M_ONCE : M_HOLD;
            bind->act.cps = 10;
            bind->act.count = 1;
            bind->act.profile = -1;
        } else if (bind) {
            struct action *a = &bind->act;

            if (!strcmp(kw, "dev")) {
                free(bind->devfilter);
                bind->devfilter = strdup(skip_words(line, 1));
            } else if (!strcmp(kw, "out")) {
                if (a->n_outs < MAX_OUTS)
                    a->outs[a->n_outs++] = atoi(w1);
            } else if (!strcmp(kw, "cmd")) {
                free(a->cmd);
                a->cmd = strdup(skip_words(line, 1));
            } else if (!strcmp(kw, "opt")) {
                if (!strcmp(w1, "mode"))             a->mode = parse_mode(w2);
                else if (!strcmp(w1, "cps"))         a->cps = strtod(w2, NULL);
                else if (!strcmp(w1, "press_ms"))    a->press_ns = ms_to_ns(w2);
                else if (!strcmp(w1, "jitter"))      a->jitter = strtod(w2, NULL) / 100.0;
                else if (!strcmp(w1, "burst"))       a->burst = atoi(w2);
                else if (!strcmp(w1, "count"))       a->count = atoi(w2);
                else if (!strcmp(w1, "loop_gap_ms")) a->loop_gap_ns = ms_to_ns(w2);
                else if (!strcmp(w1, "profile"))     a->profile = atoi(w2);
                if (a->jitter < 0)    a->jitter = 0;
                if (a->jitter > 0.95) a->jitter = 0.95;
            } else if (!strcmp(kw, "step")) {
                struct step *s = add_step(a);
                if (!strcmp(w1, "tap")) {
                    s->type = S_TAP;
                    s->a = atoi(w2);
                    s->b = ms_to_ns(w3);
                } else if (!strcmp(w1, "down")) {
                    s->type = S_DOWN;
                    s->a = atoi(w2);
                } else if (!strcmp(w1, "up")) {
                    s->type = S_UP;
                    s->a = atoi(w2);
                } else if (!strcmp(w1, "delay")) {
                    s->type = S_DELAY;
                    s->a = ms_to_ns(w2);
                    s->b = ms_to_ns(w3);
                } else if (!strcmp(w1, "move")) {
                    char w4[64] = "0";
                    sscanf(line, "%*s %*s %*s %*s %63s", w4);
                    s->type = S_MOVE;
                    s->a = atoi(w2);
                    s->b = atoi(w3);
                    s->c = ms_to_ns(w4);
                } else if (!strcmp(w1, "wheel")) {
                    s->type = S_WHEEL;
                    s->a = atoi(w2);
                    s->b = atoi(w3);
                } else if (!strcmp(w1, "cmd")) {
                    s->type = S_CMD;
                    s->str = strdup(skip_words(line, 2));
                } else {
                    a->n_steps--;
                    logmsg("config line %d: unknown step '%s'", lineno, w1);
                }
            }
        }
    }
    free(line);
    fclose(f);

    /* Arrays are final now; safe to initialise the sync objects in place. */
    for (int p = 0; p < n_profiles; p++) {
        for (int i = 0; i < profiles[p].n; i++) {
            struct action *a = &profiles[p].b[i].act;
            pthread_condattr_t ca;

            pthread_mutex_init(&a->mtx, NULL);
            pthread_condattr_init(&ca);
            pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
            pthread_cond_init(&a->cond, &ca);
            pthread_condattr_destroy(&ca);
            a->sync_ready = 1;
            a->rng = 0x9E3779B97F4A7C15ULL ^ ((uint64_t)(uintptr_t)a * 2654435761ULL) ^ (uint64_t)time(NULL);
            if (!a->rng)
                a->rng = 1;
            if (a->n_outs == 0 && a->type == A_TURBO)
                a->outs[a->n_outs++] = BTN_LEFT;
        }
    }

    base_profile = (active >= 0 && active < n_profiles) ? active : 0;
    return 0;
}


/* ---------------------------------------------------------
 * Devices
 * --------------------------------------------------------- */

static int spec_matches(const struct spec *s, const char *name, int vendor, int product)
{
    if (strcmp(s->name, name) != 0)
        return 0;
    if (s->vendor && s->vendor != vendor)
        return 0;
    if (s->product && s->product != product)
        return 0;
    return 1;
}

static int wanted(const char *name, int vendor, int product)
{
    for (int i = 0; i < n_specs; i++)
        if (spec_matches(&specs[i], name, vendor, product))
            return 1;
    return 0;
}

static int dev_wanted(const struct dev *d)
{
    struct input_id id;

    if (ioctl(d->fd, EVIOCGID, &id) < 0)
        return 0;
    return wanted(d->name, id.vendor, id.product);
}

static void drop_device(int idx, const char *reason)
{
    struct dev *d = devs[idx];

    if (!d)
        return;

    /* Release whatever this device was holding. */
    for (int c = 0; c < KEY_CNT; c++) {
        if (d->held_kind[c] == HELD_ACT)
            act_release(d->held_act[c]);
        else if (d->held_kind[c] == HELD_PASS && d->clone_fd < 0)
            out_key(c, 0, 1);
    }
    for (int s = 0; s < 4; s++)
        if (d->hat_kind[s] == HELD_ACT)
            act_release(d->hat_act[s]);

    if (d->clone_fd >= 0) {
        pthread_mutex_lock(&out_mtx);
        ioctl(d->clone_fd, UI_DEV_DESTROY);
        close(d->clone_fd);
        d->clone_fd = -1;
        pthread_mutex_unlock(&out_mtx);
    }

    ioctl(d->fd, EVIOCGRAB, 0);
    close(d->fd);
    logmsg("released %s%s%s", d->name, reason ? " - " : "", reason ? reason : "");
    free(d);
    devs[idx] = NULL;
    send_status();
}

/*
 * Opening a device node isn't free: some (audio jacks, i2c touchpads) wake
 * hardware and take 10-20 ms each, and the main loop - which moves the
 * mouse - waits meanwhile. So each node is opened once to learn its name;
 * after that only new nodes (announced by inotify on /dev/input), devices
 * the config wants, and busy devices being retried are opened again.
 */
#define MAX_SEEN 256

struct seen {
    char path[64];
    char name[NAME_LEN];
    int vendor, product;
};

static struct seen seen[MAX_SEEN];
static int n_seen;

static struct seen *seen_find(const char *path)
{
    for (int i = 0; i < n_seen; i++)
        if (!strcmp(seen[i].path, path))
            return &seen[i];
    return NULL;
}

static void seen_forget(const char *path)
{
    struct seen *s = seen_find(path);
    if (s)
        *s = seen[--n_seen];
}

static void seen_add(const char *path, const char *name, int vendor, int product)
{
    struct seen *s = seen_find(path);

    if (!s) {
        if (n_seen >= MAX_SEEN)
            return;
        s = &seen[n_seen++];
    }
    snprintf(s->path, sizeof(s->path), "%s", path);
    snprintf(s->name, sizeof(s->name), "%s", name);
    s->vendor = vendor;
    s->product = product;
}

static int failed_index(const char *path)
{
    for (int i = 0; i < n_failed; i++)
        if (!strcmp(failed[i].path, path))
            return i;
    return -1;
}

static void failed_forget(const char *path)
{
    int i = failed_index(path);
    if (i >= 0)
        failed[i] = failed[--n_failed];
}

/* Grab one /dev/input/eventN if the config wants it. Returns 1 if added. */
static int probe_node(const char *path)
{
    char name[NAME_LEN] = "";
    unsigned long relbits[BITS_LEN(REL_MAX)] = { 0 };
    unsigned long evbits[BITS_LEN(EV_MAX)] = { 0 };
    struct input_id id;
    struct seen *s = seen_find(path);
    int fd, slot = -1;

    for (int i = 0; i < MAX_DEVICES; i++) {
        if (devs[i] && !strcmp(devs[i]->path, path))
            return 0;
        if (!devs[i] && slot < 0)
            slot = i;
    }
    if (slot < 0)
        return 0;

    /* Known and not wanted: don't touch the hardware. */
    if (s && (is_own_device(s->name) || !wanted(s->name, s->vendor, s->product))) {
        failed_forget(path);
        return 0;
    }

    /* RDWR so LED state can be written back; fall back to read-only. */
    fd = open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return 0;       /* not ours to read (yet): udev may still be setting it up */

    if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) < 0 || ioctl(fd, EVIOCGID, &id) < 0) {
        close(fd);
        return 0;
    }
    seen_add(path, name, id.vendor, id.product);

    if (is_own_device(name) || !wanted(name, id.vendor, id.product)) {
        close(fd);
        failed_forget(path);
        return 0;
    }

    if (ioctl(fd, EVIOCGRAB, 1) < 0) {
        if (failed_index(path) < 0) {
            logmsg("cannot grab %s (%s): %s%s", name, path, strerror(errno),
                   errno == EBUSY ? " - another program has it (e.g. mouse_invert)" : "");
            if (n_failed < MAX_DEVICES) {
                snprintf(failed[n_failed].path, sizeof(failed[0].path), "%s", path);
                snprintf(failed[n_failed].name, sizeof(failed[0].name), "%s", name);
                n_failed++;
            }
        }
        close(fd);
        return 0;
    }
    failed_forget(path);

    struct dev *d = calloc(1, sizeof(*d));
    d->fd = fd;
    d->clone_fd = -1;
    snprintf(d->path, sizeof(d->path), "%s", path);
    snprintf(d->name, sizeof(d->name), "%s", name);

    ioctl(fd, EVIOCGBIT(0, sizeof(evbits)), evbits);
    d->has_led = test_bit(evbits, EV_LED);
    if (ioctl(fd, EVIOCGBIT(EV_REL, sizeof(relbits)), relbits) >= 0) {
        d->hires_w = test_bit(relbits, REL_WHEEL_HI_RES);
        d->hires_hw = test_bit(relbits, REL_HWHEEL_HI_RES);
    }

    if (needs_clone(fd)) {
        d->clone_fd = clone_create(fd, name);
        if (d->clone_fd < 0) {
            ioctl(fd, EVIOCGRAB, 0);
            close(fd);
            free(d);
            return 0;
        }
    }

    devs[slot] = d;
    logmsg("grabbed %s (%s)%s", name, path, d->clone_fd >= 0 ? " [own clone]" : "");
    return 1;
}

static void scan_devices(void)
{
    DIR *dir = opendir("/dev/input");
    struct dirent *de;
    int added = 0, failed_before = n_failed;

    if (!dir)
        return;
    while ((de = readdir(dir))) {
        char path[64];
        if (strncmp(de->d_name, "event", 5) != 0)
            continue;
        snprintf(path, sizeof(path), "/dev/input/%.40s", de->d_name);
        added |= probe_node(path);
    }
    closedir(dir);

    if (added)
        out_sync_caps();
    if (added || n_failed != failed_before)
        send_status();
}

/* Devices another program had grabbed: try those (and only those) again. */
static void retry_failed(void)
{
    char paths[MAX_DEVICES][64];
    int n = n_failed, added = 0;

    for (int i = 0; i < n; i++)
        memcpy(paths[i], failed[i].path, sizeof(paths[i]));
    for (int i = 0; i < n; i++)
        added |= probe_node(paths[i]);
    if (added)
        out_sync_caps();
    if (added || n_failed != n)
        send_status();
}

/* A node appeared, changed permissions or went away (inotify on /dev/input). */
static void device_node_event(const char *name, uint32_t mask)
{
    char path[64];
    int failed_before = n_failed;

    if (strncmp(name, "event", 5) != 0)
        return;
    snprintf(path, sizeof(path), "/dev/input/%.40s", name);
    seen_forget(path);          /* the node may now be a different device */
    if (mask & IN_DELETE) {
        failed_forget(path);
    } else if (probe_node(path)) {
        out_sync_caps();
        send_status();
        return;
    }
    if (n_failed != failed_before)
        send_status();
}

static void sync_devices(void)
{
    for (int i = 0; i < MAX_DEVICES; i++)
        if (devs[i] && !dev_wanted(devs[i]))
            drop_device(i, "removed from config");
    scan_devices();
    out_sync_caps();
}

static void reload(void)
{
    stop_all_actions();
    out_release_all();
    if (parse_config() < 0)
        return;
    config_loaded = 1;
    n_failed = 0;
    sync_devices();
    logmsg("config loaded (%d profile%s, active: %s)", n_profiles, n_profiles == 1 ? "" : "s",
           n_profiles ? profiles[base_profile].name : "-");
    send_status();
}

/* Caps lock etc. set by the compositor: copy to the real keyboards. */
static void forward_leds(int from_fd, struct dev *only)
{
    struct input_event evs[16];
    ssize_t got;

    while ((got = read(from_fd, evs, sizeof(evs))) > 0) {
        for (size_t e = 0; e < (size_t)got / sizeof(evs[0]); e++) {
            if (evs[e].type != EV_LED)
                continue;
            for (int i = 0; i < MAX_DEVICES; i++) {
                struct dev *d = devs[i];
                if (!d || !d->has_led)
                    continue;
                if (only ? d != only : d->clone_fd >= 0)
                    continue;
                raw_write(d->fd, EV_LED, evs[e].code, evs[e].value);
                raw_write(d->fd, EV_SYN, SYN_REPORT, 0);
            }
        }
    }
}


/* ---------------------------------------------------------
 * Event handling
 * --------------------------------------------------------- */

static void handle_key(struct dev *d, int code, int value)
{
    struct binding *b;

    if (value == 2)     /* kernel autorepeat; compositors repeat on their own */
        return;
    if (code < 0 || code >= KEY_CNT)
        return;

    send_input(d, code, value);

    if (value) {
        b = lookup(code, d->name);
        if (!b) {
            d->held_kind[code] = HELD_PASS;
            pass_key(d, code, 1);
        } else {
            d->held_kind[code] = HELD_ACT;
            d->held_act[code] = &b->act;
            act_press(&b->act);
        }
        return;
    }

    switch (d->held_kind[code]) {
    case HELD_PASS:
        pass_key(d, code, 0);
        break;
    case HELD_ACT:
        act_release(d->held_act[code]);
        break;
    default:
        /* Press happened before a reload; make sure nothing stays stuck. */
        if (pass_is_down(d, code))
            pass_key(d, code, 0);
        break;
    }
    d->held_kind[code] = HELD_NONE;
    d->held_act[code] = NULL;
}

static void handle_rel(struct dev *d, int code, int value)
{
    const struct movement *mv = active_movement();

    if (code == REL_X || code == REL_Y) {
        double sens;

        if (mv->swap_xy)
            code = code == REL_X ? REL_Y : REL_X;

        if (code == REL_X) {
            if (mv->invert_x)
                value = -value;
            sens = mv->sens_x;
        } else {
            if (mv->invert_y)
                value = -value;
            sens = mv->sens_y;
        }

        if (sens != 1.0) {
            /* Keep the fractional remainder so slow movements aren't lost. */
            double *acc = code == REL_X ? &acc_x : &acc_y;
            *acc += value * sens;
            value = (int)*acc;
            *acc -= value;
            if (!value)
                return;
        }
        pass_rel(d, code, value);
        return;
    }

    int horizontal, lowres;
    switch (code) {
    case REL_WHEEL:         horizontal = 0; lowres = 1; break;
    case REL_WHEEL_HI_RES:  horizontal = 0; lowres = 0; break;
    case REL_HWHEEL:        horizontal = 1; lowres = 1; break;
    case REL_HWHEEL_HI_RES: horizontal = 1; lowres = 0; break;
    default:
        pass_rel(d, code, value);
        return;
    }
    if (!value)
        return;

    int trig = horizontal ? (value > 0 ? W_RIGHT : W_LEFT) : (value > 0 ? W_UP : W_DOWN);
    struct binding *b = lookup(trig, d->name);

    if (lowres)
        send_input(d, trig, 1);

    if (b) {
        /* Fire once per notch on the low-res event; swallow hi-res. */
        if (lowres) {
            for (int i = 0; i < abs(value); i++) {
                act_press(&b->act);
                act_release(&b->act);
            }
        }
        return;
    }

    if (horizontal ? mv->invert_hwheel : mv->invert_wheel)
        value = -value;
    if (!lowres && d->clone_fd < 0 && !(horizontal ? out_hires_hw : out_hires_w))
        return;
    pass_rel(d, code, value);
}

/* d-pad axes become D_* triggers; unbound directions pass through. */
static void handle_hat(struct dev *d, int axis, int code, int value)
{
    int neg = axis ? D_UP : D_LEFT;
    int pos = axis ? D_DOWN : D_RIGHT;
    int old = d->hat[axis];
    int fwd = 0;

    if (value < 0)
        value = -1;
    else if (value > 0)
        value = 1;
    if (value == old)
        return;
    d->hat[axis] = value;

    if (old) {
        int t = old < 0 ? neg : pos, s = -t - 5;
        send_input(d, t, 0);
        if (d->hat_kind[s] == HELD_ACT)
            act_release(d->hat_act[s]);
        d->hat_kind[s] = HELD_NONE;
        d->hat_act[s] = NULL;
    }

    if (value) {
        int t = value < 0 ? neg : pos, s = -t - 5;
        struct binding *b = lookup(t, d->name);

        send_input(d, t, 1);
        if (b) {
            d->hat_kind[s] = HELD_ACT;
            d->hat_act[s] = &b->act;
            act_press(&b->act);
        } else {
            d->hat_kind[s] = HELD_PASS;
            fwd = value;
        }
    }

    if (fwd != d->hat_fwd[axis]) {
        d->hat_fwd[axis] = fwd;
        pass_raw(d, EV_ABS, code, fwd);
    }
}

static void handle_event(struct dev *d, const struct input_event *ev)
{
    switch (ev->type) {
    case EV_SYN:
        if (d->clone_fd >= 0) {
            if (ev->code == SYN_REPORT || ev->code == SYN_MT_REPORT)
                pass_raw(d, EV_SYN, ev->code, ev->value);
        } else if (ev->code == SYN_REPORT) {
            out_syn();
        }
        break;
    case EV_KEY:
        handle_key(d, ev->code, ev->value);
        break;
    case EV_REL:
        handle_rel(d, ev->code, ev->value);
        break;
    case EV_ABS:
        if (d->clone_fd < 0)
            break;
        if (ev->code == ABS_HAT0X || ev->code == ABS_HAT0Y)
            handle_hat(d, ev->code == ABS_HAT0Y, ev->code, ev->value);
        else
            pass_raw(d, EV_ABS, ev->code, ev->value);
        break;
    case EV_MSC:
        if (d->clone_fd >= 0)
            pass_raw(d, EV_MSC, ev->code, ev->value);
        break;
    default:
        break;
    }
}


/* ---------------------------------------------------------
 * Commands
 * --------------------------------------------------------- */

static void command(char *line)
{
    char cmd[32] = "", arg[64] = "";

    if (sscanf(line, "%31s %63s", cmd, arg) < 1)
        return;
    if (!strcmp(cmd, "reload"))
        reload();
    else if (!strcmp(cmd, "next") || !strcmp(cmd, "cycle"))
        cycle_profile(1);
    else if (!strcmp(cmd, "prev"))
        cycle_profile(-1);
    else if (!strcmp(cmd, "profile"))
        switch_profile(atoi(arg));
    else if (!strcmp(cmd, "status"))
        send_status();
    else if (!strcmp(cmd, "quit"))
        running = 0;
    else
        logmsg("unknown command: %s", line);
}

/* "all_bind next" etc: tell the running engine and exit. */
static int is_client_cmd(const char *arg)
{
    return !strcmp(arg, "next") || !strcmp(arg, "prev") || !strcmp(arg, "cycle")
        || !strcmp(arg, "reload");
}

static int pid_is_engine(pid_t pid)
{
    char path[64], comm[64] = "", cmdline[512];
    FILE *f;
    size_t n;

    snprintf(path, sizeof(path), "/proc/%d/comm", pid);
    f = fopen(path, "r");
    if (!f)
        return 0;
    if (!fgets(comm, sizeof(comm), f))
        comm[0] = '\0';
    fclose(f);
    comm[strcspn(comm, "\n")] = '\0';
    if (strcmp(comm, ENGINE_NAME) != 0)
        return 0;

    /* Exited but not yet reaped (zombie) doesn't count. */
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    f = fopen(path, "r");
    if (f) {
        char stat[512] = "", *paren;
        if (!fgets(stat, sizeof(stat), f))
            stat[0] = '\0';
        fclose(f);
        paren = strrchr(stat, ')');
        if (paren && paren[1] == ' ' && paren[2] == 'Z')
            return 0;
    }

    /* Short-lived "all_bind next" clients aren't engines. */
    snprintf(path, sizeof(path), "/proc/%d/cmdline", pid);
    f = fopen(path, "r");
    if (!f)
        return 1;
    n = fread(cmdline, 1, sizeof(cmdline) - 1, f);
    fclose(f);
    cmdline[n] = '\0';
    if (strlen(cmdline) + 1 < n)
        return !is_client_cmd(cmdline + strlen(cmdline) + 1);
    return 1;
}

/* Only one engine may own the devices: stop any other all_bind. */
static void kill_previous(void)
{
    pid_t pids[64];
    int n = 0;
    DIR *dir = opendir("/proc");
    struct dirent *de;

    if (!dir)
        return;
    while ((de = readdir(dir)) && n < 64) {
        pid_t pid = (pid_t)atoi(de->d_name);
        if (pid > 0 && pid != getpid() && pid_is_engine(pid))
            pids[n++] = pid;
    }
    closedir(dir);

    for (int i = 0; i < n; i++) {
        logmsg("stopping other %s (pid %d)", ENGINE_NAME, pids[i]);
        if (kill(pids[i], SIGTERM) < 0 && errno == EPERM)
            logmsg("pid %d belongs to another user (system service?) - stop it first", pids[i]);
    }
    for (int t = 0; t < 40 && n; t++) {
        int alive = 0;
        usleep(50000);
        for (int i = 0; i < n; i++)
            if (kill(pids[i], 0) == 0 && pid_is_engine(pids[i]))
                alive = 1;
        if (!alive)
            return;
    }
    for (int i = 0; i < n; i++)
        if (pid_is_engine(pids[i]))
            kill(pids[i], SIGKILL);
    usleep(100000);
}


/* ---------------------------------------------------------
 * Config folder watch
 *
 * The engine reloads by itself whenever all_bind.conf is replaced (the GUI
 * and CLI save it), and runs commands dropped in as ".command-<pid>" files.
 * That works even when it runs as a root service the user can't signal.
 * /dev/input is watched the same way, so plugged-in devices are noticed
 * without polling.
 * --------------------------------------------------------- */

static int ino_fd = -1;
static int ino_wd = -1;
static int ino_wd_dev = -1;

static void watch_config_dir(void)
{
    if (ino_fd < 0)
        ino_fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (ino_fd >= 0 && ino_wd < 0)
        ino_wd = inotify_add_watch(ino_fd, config_dir,
                                   IN_CLOSE_WRITE | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF);
    if (ino_fd >= 0 && ino_wd_dev < 0)
        ino_wd_dev = inotify_add_watch(ino_fd, "/dev/input", IN_CREATE | IN_ATTRIB | IN_DELETE);
}

static int file_owner_ok(const struct stat *st)
{
    uid_t want = run_uid != (uid_t)-1 ? run_uid : getuid();
    return st->st_uid == want || st->st_uid == 0;
}

static void handle_command_file(const char *name)
{
    char path[PATH_MAX], buf[64] = "";
    struct stat st;
    ssize_t n;
    int fd;

    snprintf(path, sizeof(path), "%.3800s/%.200s", config_dir, name);
    fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && file_owner_ok(&st)) {
        n = read(fd, buf, sizeof(buf) - 1);
        buf[n > 0 ? n : 0] = '\0';
    }
    close(fd);
    unlink(path);

    buf[strcspn(buf, "\r\n")] = '\0';
    if (!strcmp(buf, "next") || !strcmp(buf, "cycle"))
        cycle_profile(1);
    else if (!strcmp(buf, "prev"))
        cycle_profile(-1);
    else if (!strcmp(buf, "reload"))
        reload();
    else if (!strcmp(buf, "quit"))
        running = 0;        /* lets the user stop an engine running as root */
    else if (buf[0])
        logmsg("ignored command file: %s", buf);
}

/* Returns 1 if all_bind.conf changed. */
static int read_inotify(void)
{
    char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
    const char *conf_name = strrchr(config_path, '/');
    int changed = 0;
    ssize_t n;

    conf_name = conf_name ? conf_name + 1 : config_path;

    while ((n = read(ino_fd, buf, sizeof(buf))) > 0) {
        for (char *p = buf; p < buf + n;) {
            const struct inotify_event *ev = (const struct inotify_event *)p;

            if (ev->wd == ino_wd_dev && ino_wd_dev >= 0) {
                if (ev->mask & IN_IGNORED)
                    ino_wd_dev = -1;
                else if (ev->len)
                    device_node_event(ev->name, ev->mask);
            } else if (ev->mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF))
                ino_wd = -1;        /* folder gone; re-watched on the next tick */
            else if (ev->len && !strcmp(ev->name, conf_name))
                changed = 1;
            else if (ev->len && starts_with(ev->name, ".command-"))
                handle_command_file(ev->name);
            p += sizeof(*ev) + ev->len;
        }
    }
    return changed;
}


/* ---------------------------------------------------------
 * Command line
 * --------------------------------------------------------- */

static void usage(void)
{
    puts("usage:\n"
         "  all_bind [--user NAME] [--events] [CONFIG]   run the engine\n"
         "  all_bind next | prev                        switch profile (with a notification)\n"
         "  all_bind reload                             reload the config\n"
         "  all_bind --cli [COMMAND ...]                configure from the terminal\n"
         "  all_bind --gui                              open the app\n"
         "\n"
         "The config is ~/.config/all_bind/all_bind.conf (written by the GUI / CLI).\n"
         "--user NAME  run as root (system service) with NAME's config; programs it\n"
         "             starts (commands, notifications) run as NAME.\n"
         "--events     report physical input on stdout (used by the GUI).");
}

static int resolve_paths(const char *conf_arg, const char *user)
{
    if (user) {
        struct passwd *pw = getpwnam(user);

        if (!pw) {
            fprintf(stderr, "all_bind: no such user: %s\n", user);
            return -1;
        }
        snprintf(run_user, sizeof(run_user), "%s", pw->pw_name);
        snprintf(run_home, sizeof(run_home), "%s", pw->pw_dir);
        snprintf(config_dir, sizeof(config_dir), "%.3000s/.config/all_bind", pw->pw_dir);
        if (geteuid() == 0) {
            run_uid = pw->pw_uid;
            run_gid = pw->pw_gid;
            run_ngroups = (int)(sizeof(run_groups) / sizeof(run_groups[0]));
            if (getgrouplist(pw->pw_name, pw->pw_gid, run_groups, &run_ngroups) < 0)
                run_ngroups = (int)(sizeof(run_groups) / sizeof(run_groups[0]));
        }
    } else {
        const char *xdg = getenv("XDG_CONFIG_HOME");
        const char *home = getenv("HOME");

        if (xdg && *xdg)
            snprintf(config_dir, sizeof(config_dir), "%.3000s/all_bind", xdg);
        else
            snprintf(config_dir, sizeof(config_dir), "%.3000s/.config/all_bind", home ? home : "");
    }

    if (conf_arg) {
        if (!realpath(conf_arg, config_path))
            snprintf(config_path, sizeof(config_path), "%s", conf_arg);
        char *slash = strrchr(config_path, '/');
        if (slash)
            snprintf(config_dir, sizeof(config_dir), "%.*s", (int)(slash - config_path), config_path);
    } else {
        snprintf(config_path, sizeof(config_path), "%.3800s/all_bind.conf", config_dir);
    }
    return 0;
}

static int client_send(const char *cmd)
{
    char tmp[PATH_MAX], final[PATH_MAX];
    DIR *dir = opendir("/proc");
    struct dirent *de;
    int found = 0, fd;

    while (dir && (de = readdir(dir))) {
        pid_t pid = (pid_t)atoi(de->d_name);
        if (pid > 0 && pid != getpid() && pid_is_engine(pid))
            found = 1;
    }
    if (dir)
        closedir(dir);

    if (!found) {
        fprintf(stderr, "all_bind: not running\n");
        notify(ENGINE_NAME, "Not running - nothing to switch");
        usleep(50000);
        return 1;
    }

    snprintf(tmp, sizeof(tmp), "%.3800s/.cmd-%d.tmp", config_dir, (int)getpid());
    snprintf(final, sizeof(final), "%.3800s/.command-%d", config_dir, (int)getpid());
    fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0 || dprintf(fd, "%s\n", cmd) < 0 || close(fd) < 0 || rename(tmp, final) < 0) {
        fprintf(stderr, "all_bind: cannot write to %s: %s\n", config_dir, strerror(errno));
        unlink(tmp);
        return 1;
    }
    return 0;
}

/*
 * Can we read input devices and create virtual ones without root?
 * True for members of the input group (plus uinput access, which the udev
 * rule gives the logged-in user).
 */
static int have_device_access(void)
{
    DIR *dir;
    struct dirent *de;
    int readable = 0;

    if (access("/dev/uinput", R_OK | W_OK) != 0)
        return 0;
    dir = opendir("/dev/input");
    if (!dir)
        return 0;
    while (!readable && (de = readdir(dir))) {
        char path[64];
        if (strncmp(de->d_name, "event", 5) != 0)
            continue;
        snprintf(path, sizeof(path), "/dev/input/%.40s", de->d_name);
        readable = access(path, R_OK) == 0;
    }
    closedir(dir);
    return readable;
}

/*
 * No access: ask for the password (polkit) and run as root with this
 * user's config instead. Programs the engine starts still run as the user.
 */
static void elevate_if_needed(int argc, char **argv, const char *conf_arg)
{
    char exe[PATH_MAX], conf[PATH_MAX];
    char *args[300];
    struct passwd *pw;
    ssize_t len;
    int n = 0;

    if (geteuid() == 0 || have_device_access() || getenv("ALL_BIND_NO_ELEVATE"))
        return;
    pw = getpwuid(getuid());
    len = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (!pw || len <= 0)
        return;
    exe[len] = '\0';

    logmsg("no access to input devices (not in the input group?) - asking for your password");

    args[n++] = "pkexec";
    args[n++] = exe;
    args[n++] = "--user";
    args[n++] = pw->pw_name;
    for (int i = 1; i < argc && n < 298; i++) {
        /* pkexec changes directory: pass the config as an absolute path. */
        if (conf_arg && argv[i] == conf_arg && realpath(conf_arg, conf))
            args[n++] = conf;
        else
            args[n++] = argv[i];
    }
    args[n] = NULL;

    fflush(stdout);
    execvp("pkexec", args);
    logmsg("pkexec failed (%s) - add yourself to the input group: sudo usermod -aG input $USER",
           strerror(errno));
}

/* --cli / --gui: hand over to all_bind-gui (all_bind_gui.py). */
static int exec_frontend(int cli, int argc, char **argv)
{
    char exe[PATH_MAX], path[PATH_MAX + 32];
    char *args[256];
    int n = 0;
    ssize_t len = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    static const char *const names[] = { "all_bind_gui.py", "all_bind-gui" };

    args[n++] = "all_bind-gui";
    if (cli)
        args[n++] = "--cli";
    for (int i = 0; i < argc && n < 254; i++)
        args[n++] = argv[i];
    args[n] = NULL;

    /* Next to this binary first (development folder), then PATH. */
    if (len > 0) {
        exe[len] = '\0';
        char *slash = strrchr(exe, '/');
        if (slash)
            *slash = '\0';
        for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
            snprintf(path, sizeof(path), "%s/%s", exe, names[i]);
            if (access(path, X_OK) == 0)
                execv(path, args);
        }
    }
    execvp("all_bind-gui", args);
    fprintf(stderr, "all_bind: all_bind-gui not found (%s)\n", strerror(errno));
    return 127;
}


/* ---------------------------------------------------------
 * Main loop
 * --------------------------------------------------------- */

int main(int argc, char **argv)
{
    const char *conf_arg = NULL, *user = NULL;
    sigset_t mask;
    int sfd, stdin_open = 1, reload_pending = 0;
    char inbuf[4096];
    size_t inlen = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];

        if (!strcmp(a, "--cli"))
            return exec_frontend(1, argc - i - 1, argv + i + 1);
        if (!strcmp(a, "--gui"))
            return exec_frontend(0, argc - i - 1, argv + i + 1);
        if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            usage();
            return 0;
        }
        if ((!strcmp(a, "--user") || !strcmp(a, "-u")) && i + 1 < argc)
            user = argv[++i];
        else if (!strcmp(a, "--events"))
            send_events = 1;
        else if (i == 1 && is_client_cmd(a)) {
            if (resolve_paths(NULL, NULL) < 0)
                return 1;
            signal(SIGCHLD, SIG_IGN);
            return client_send(a);
        } else if (a[0] == '-') {
            fprintf(stderr, "all_bind: unknown option %s\n\n", a);
            usage();
            return 2;
        } else {
            conf_arg = a;
        }
    }

    if (!user)
        elevate_if_needed(argc, argv, conf_arg);

    if (resolve_paths(conf_arg, user) < 0)
        return 1;

    /* Tighter sleeps for the turbo / macro threads (inherited). */
    prctl(PR_SET_TIMERSLACK, 1000UL, 0, 0, 0);

    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_IGN);

    /* Block before any thread exists so only the signalfd sees them. */
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGHUP);
    sigaddset(&mask, SIGUSR1);
    sigaddset(&mask, SIGUSR2);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    sfd = signalfd(-1, &mask, SFD_CLOEXEC | SFD_NONBLOCK);

    if (geteuid() == 0 && !user)
        logmsg("running as root without --user: using %s", config_path);

    kill_previous();
    watch_config_dir();

    config_loaded = parse_config() == 0;
    scan_devices();
    out_sync_caps();
    if (out_fd < 0)
        return 1;
    logmsg("engine running (%d profile%s, active: %s, config: %s)", n_profiles,
           n_profiles == 1 ? "" : "s", n_profiles ? profiles[base_profile].name : "-", config_path);
    send_status();

    while (running) {
        struct pollfd pfd[2 * MAX_DEVICES + 4];
        int idx[2 * MAX_DEVICES + 4];
        int n = 0;

        pfd[n].fd = sfd;
        pfd[n].events = POLLIN;
        idx[n++] = -1;
        if (stdin_open) {
            pfd[n].fd = STDIN_FILENO;
            pfd[n].events = POLLIN;
            idx[n++] = -2;
        }
        if (out_fd >= 0) {
            pfd[n].fd = out_fd;
            pfd[n].events = POLLIN;
            idx[n++] = -3;
        }
        if (ino_fd >= 0) {
            pfd[n].fd = ino_fd;
            pfd[n].events = POLLIN;
            idx[n++] = -4;
        }
        for (int i = 0; i < MAX_DEVICES; i++) {
            if (!devs[i])
                continue;
            pfd[n].fd = devs[i]->fd;
            pfd[n].events = POLLIN;
            idx[n++] = i;
            if (devs[i]->clone_fd >= 0) {
                pfd[n].fd = devs[i]->clone_fd;
                pfd[n].events = POLLIN;
                idx[n++] = 1000 + i;
            }
        }

        int r = poll(pfd, n, 2000);
        if (r < 0 && errno != EINTR)
            break;

        for (int k = 0; r > 0 && k < n; k++) {
            if (!pfd[k].revents)
                continue;

            if (idx[k] == -1) {
                struct signalfd_siginfo si;
                while (read(sfd, &si, sizeof(si)) == sizeof(si)) {
                    if (si.ssi_signo == SIGHUP)
                        reload_pending = 1;
                    else if (si.ssi_signo == SIGUSR1)
                        cycle_profile(1);
                    else if (si.ssi_signo == SIGUSR2)
                        cycle_profile(-1);
                    else
                        running = 0;
                }
            } else if (idx[k] == -2) {
                ssize_t got = read(STDIN_FILENO, inbuf + inlen, sizeof(inbuf) - 1 - inlen);
                if (got <= 0) {
                    stdin_open = 0;     /* GUI closed: keep running headless */
                    continue;
                }
                inlen += got;
                inbuf[inlen] = '\0';
                char *nl;
                while ((nl = strchr(inbuf, '\n'))) {
                    *nl = '\0';
                    command(inbuf);
                    inlen -= (nl + 1 - inbuf);
                    memmove(inbuf, nl + 1, inlen + 1);
                }
                if (inlen >= sizeof(inbuf) - 1)
                    inlen = 0;
            } else if (idx[k] == -3) {
                if (out_fd >= 0)
                    forward_leds(out_fd, NULL);
            } else if (idx[k] == -4) {
                if (read_inotify())
                    reload_pending = 1;
            } else if (idx[k] >= 1000) {
                struct dev *d = devs[idx[k] - 1000];
                if (d && d->clone_fd >= 0)
                    forward_leds(d->clone_fd, d);
            } else {
                struct dev *d = devs[idx[k]];
                struct input_event evs[64];

                if (!d)
                    continue;
                if (pfd[k].revents & (POLLERR | POLLHUP | POLLNVAL)) {
                    drop_device(idx[k], "disconnected");
                    continue;
                }
                for (;;) {
                    ssize_t got = read(d->fd, evs, sizeof(evs));
                    if (got < 0) {
                        if (errno != EAGAIN && errno != EINTR)
                            drop_device(idx[k], "disconnected");
                        break;
                    }
                    for (size_t e = 0; e < (size_t)got / sizeof(evs[0]); e++) {
                        handle_event(d, &evs[e]);
                        if (!devs[idx[k]])
                            break;
                    }
                    if (!devs[idx[k]] || (size_t)got < sizeof(evs))
                        break;
                }
            }
        }

        /* One reload per burst of saves / editor writes. */
        if (reload_pending) {
            reload_pending = 0;
            reload();
        }

        /*
         * Light housekeeping only - nothing here opens devices except a busy
         * one being retried. New devices arrive through inotify.
         */
        if (time(NULL) - last_scan >= 2) {
            last_scan = time(NULL);
            if (ino_wd < 0 || ino_wd_dev < 0)
                watch_config_dir();
            if (!config_loaded && access(config_path, R_OK) == 0)
                reload();
            else if (n_failed)
                retry_failed();
            else if (ino_wd_dev < 0)
                scan_devices();     /* no inotify: readdir; known nodes aren't reopened */
        }
    }

    stop_all_actions();
    for (int i = 0; i < MAX_DEVICES; i++)
        drop_device(i, NULL);
    pthread_mutex_lock(&out_mtx);
    out_destroy_locked();
    pthread_mutex_unlock(&out_mtx);
    free_profiles();
    logmsg("engine stopped; devices released");
    return 0;
}
