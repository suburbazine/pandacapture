/* A fake RP1210 + J2534 driver DLL for testing the adapter bridge without hardware.
 *
 * Every read returns 0x316 (RPM 2000) and a 29-bit 0x18FEF100 frame; J2534 reads also return an echo
 * of a transmitted frame, which the bridge must skip. FAKE_FAIL_AFTER=N makes reads fail after N calls
 * that returned frames, like an adapter that was unplugged.
 *
 * Build: gcc -shared -O2 -o fake_driver.dll fake_driver.c   (see tests/test_adapters.py)
 */
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#define EXPORT __declspec(dllexport)

static int reads, fail_after = -1, clients;
static unsigned int clock_us;

static void init(void) {
    const char *f = getenv("FAKE_FAIL_AFTER");
    if (f) fail_after = atoi(f);
}

/* ---- RP1210 ---- */

EXPORT short WINAPI RP1210_ClientConnect(HWND hwnd, short device, const char *protocol, long tx, long rx, short pkt) {
    init();
    if (device != 1) return -129;               /* ERR_INVALID_DEVICE */
    if (strstr(protocol, "Channel=")) return -132; /* pretend this driver only takes the plain form */
    return (short)(clients++);
}

EXPORT short WINAPI RP1210_ClientDisconnect(short client) { return 0; }

EXPORT short WINAPI RP1210_SendCommand(short command, short client, char *data, short size) {
    if (command == 45 && size >= 4) { strcpy(data, "500"); return 0; }
    return 0;
}

EXPORT short WINAPI RP1210_GetErrorMsg(short code, char *text) {
    strcpy(text, code == 129 ? "ERR_INVALID_DEVICE" : "ERR_FAKE");
    return 0;
}

static int rp_pending;

EXPORT short WINAPI RP1210_ReadMessage(short client, char *buf, short size, short block) {
    /* two frames per "burst", then empty once, so the bridge sees both states */
    if (rp_pending == 0) {
        if (fail_after >= 0 && reads >= fail_after) return -142; /* hardware not responding */
        reads++;
        rp_pending = 3;
    }
    rp_pending--;
    if (rp_pending == 0) return 0;
    clock_us += 1000;
    unsigned char *b = (unsigned char *)buf;
    b[0] = clock_us >> 24; b[1] = clock_us >> 16; b[2] = clock_us >> 8; b[3] = clock_us;
    if (rp_pending == 2) {                      /* standard: type 0, 2-byte id */
        unsigned char m[] = {0, 0x03, 0x16, 0, 0x10, 0x40, 0x1F, 0, 0, 0, 0};
        memcpy(b + 4, m, sizeof m);
        return 4 + sizeof m;
    }
    unsigned char m[] = {1, 0x18, 0xFE, 0xF1, 0x00, 1, 2, 3, 4, 5, 6, 7, 8}; /* extended: type 1, 4-byte id */
    memcpy(b + 4, m, sizeof m);
    return 4 + sizeof m;
}

/* ---- J2534 04.04 ---- */

typedef struct {
    unsigned long ProtocolID, RxStatus, TxFlags, Timestamp, DataSize, ExtraDataIndex;
    unsigned char Data[4128];
} PASSTHRU_MSG;

static char last_error[80] = "";
static unsigned long connected_baud;

EXPORT long WINAPI PassThruOpen(void *name, unsigned long *device) { init(); *device = 7; return 0; }
EXPORT long WINAPI PassThruClose(unsigned long device) { return 0; }

EXPORT long WINAPI PassThruConnect(unsigned long device, unsigned long protocol, unsigned long flags,
                                   unsigned long baud, unsigned long *channel) {
    if (protocol != 5) { strcpy(last_error, "only CAN"); return 0x03; }
    if (baud != 500000 && baud != 250000) { strcpy(last_error, "unsupported baud"); return 0x1A; }
    connected_baud = baud;
    *channel = 1;
    return 0;
}

EXPORT long WINAPI PassThruDisconnect(unsigned long channel) { return 0; }

EXPORT long WINAPI PassThruStartMsgFilter(unsigned long channel, unsigned long type, PASSTHRU_MSG *mask,
                                          PASSTHRU_MSG *pattern, PASSTHRU_MSG *flow, unsigned long *id) {
    if (type != 1 || mask->DataSize != 4 || mask->ProtocolID != 5) { strcpy(last_error, "bad filter"); return 0x0A; }
    *id = 1;
    return 0;
}

static void put(PASSTHRU_MSG *m, unsigned long rx, unsigned long id, const unsigned char *d, int n) {
    memset(m, 0, 24);
    m->ProtocolID = 5;
    m->RxStatus = rx;
    m->Timestamp = (clock_us += 1000);
    m->DataSize = 4 + n;
    m->Data[0] = id >> 24; m->Data[1] = id >> 16; m->Data[2] = id >> 8; m->Data[3] = id;
    memcpy(m->Data + 4, d, n);
}

EXPORT long WINAPI PassThruReadMsgs(unsigned long channel, PASSTHRU_MSG *msgs, unsigned long *num, unsigned long timeout) {
    if (fail_after >= 0 && reads >= fail_after) { *num = 0; strcpy(last_error, "device not connected"); return 0x08; }
    if (reads++ % 2) { *num = 0; return 0x10; } /* ERR_BUFFER_EMPTY every other call */
    unsigned char rpm[] = {0, 0x10, 0x40, 0x1F, 0, 0, 0, 0};
    unsigned char ext[] = {1, 2, 3, 4, 5, 6, 7, 8};
    put(&msgs[0], 0, 0x316, rpm, 8);
    put(&msgs[1], 0x100, 0x18FEF100, ext, 8);
    put(&msgs[2], 0x01, 0x7E0, rpm, 8);         /* TX_MSG_TYPE: an echo, to be skipped */
    *num = 3;
    return 0;
}

EXPORT long WINAPI PassThruGetLastError(char *text) { strcpy(text, last_error); return 0; }

EXPORT long WINAPI PassThruReadVersion(unsigned long device, char *fw, char *dll, char *api) {
    strcpy(fw, "1.0"); strcpy(dll, "fake 2.0"); strcpy(api, "04.04");
    return 0;
}
