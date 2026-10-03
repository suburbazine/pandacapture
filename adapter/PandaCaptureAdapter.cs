// pandacapture-adapter: reads a CAN bus through a vendor's RP1210 or J2534 driver and streams the
// frames to PandaCapture over stdout. PandaCapture starts it and picks the 32-bit or 64-bit build
// to match the driver's DLL (most vendor drivers are 32-bit; PandaCapture itself is 64-bit).
//
// Receive only: no send function is ever loaded. These adapters acknowledge frames like any CAN
// node, which is harmless at the bus's real bit rate.
//
// Usage (PandaCapture runs this; it is not meant to be run by hand):
//   pandacapture-adapter rp1210 API DEVICE PROTOCOL [PROTOCOL...]   e.g. NULN2R32 1 "CAN:Baud=Auto"
//   pandacapture-adapter j2534 DLLPATH KBPS
// Closing stdin stops it.
//
// Output records, little-endian:
//   'I' u16 n, n bytes UTF-8     what it connected to (once, after connecting)
//   'N' u16 n, n bytes UTF-8     a note
//   'F' u32 microseconds (adapter clock), u32 id (bit 31: 29-bit id), u8 n, n data bytes
//   'E' u16 n, n bytes UTF-8     a fatal error; the program then exits
//
// C# 5 on purpose, so it compiles with the csc.exe that ships with Windows: see build.py.

using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace PandaCaptureAdapter
{
    static class Kernel32
    {
        [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr LoadLibrary(string path);

        [DllImport("kernel32", CharSet = CharSet.Ansi, ExactSpelling = true)]
        public static extern IntPtr GetProcAddress(IntPtr module, string name);

        [DllImport("kernel32")]
        public static extern bool FreeLibrary(IntPtr module);
    }

    class AdapterException : Exception
    {
        public AdapterException(string message) : base(message) { }
    }

    /// <summary>Anything that yields CAN frames.</summary>
    interface ICanReader : IDisposable
    {
        string Description { get; }
        /// <summary>Adds waiting frames to the output; returns how many.</summary>
        int Read(Output output);
    }

    /// <summary>The record stream to PandaCapture, buffered and flushed in batches.</summary>
    class Output
    {
        readonly Stream stream;
        readonly MemoryStream buffer = new MemoryStream();
        readonly BinaryWriter w;

        public Output(Stream stream)
        {
            this.stream = stream;
            w = new BinaryWriter(buffer);
        }

        public void Text(char kind, string text)
        {
            byte[] b = Encoding.UTF8.GetBytes(text);
            int n = Math.Min(b.Length, 60000);
            w.Write((byte)kind);
            w.Write((ushort)n);
            w.Write(b, 0, n);
            Flush();
        }

        public void Frame(uint micros, uint id, bool extended, byte[] data, int offset, int count)
        {
            if (count > 64) count = 64;
            w.Write((byte)'F');
            w.Write(micros);
            w.Write(id | (extended ? 0x80000000u : 0u));
            w.Write((byte)count);
            w.Write(data, offset, count);
        }

        public bool Pending { get { return buffer.Length > 0; } }

        public void Flush()
        {
            if (buffer.Length == 0) return;
            w.Flush();
            stream.Write(buffer.GetBuffer(), 0, (int)buffer.Length);
            stream.Flush();
            buffer.SetLength(0);
        }
    }

    static class Native
    {
        public static T Fn<T>(IntPtr dll, string name, bool required) where T : class
        {
            IntPtr p = Kernel32.GetProcAddress(dll, name);
            if (p == IntPtr.Zero)
            {
                if (required) throw new AdapterException(name + " is missing from the driver DLL.");
                return null;
            }
            return Marshal.GetDelegateForFunctionPointer(p, typeof(T)) as T;
        }

        public static IntPtr Load(string path)
        {
            IntPtr dll = Kernel32.LoadLibrary(path);
            if (dll == IntPtr.Zero)
            {
                int err = Marshal.GetLastWin32Error();
                string hint = err == 193 ? " (it's built for the other bitness: 32-bit vs 64-bit)" : "";
                throw new AdapterException(string.Format("Could not load {0}: Windows error {1}{2}. Is the adapter's driver installed?", path, err, hint));
            }
            return dll;
        }
    }

    // ---------------------------------------------------------------- RP1210 (TMC RP1210C)

    [UnmanagedFunctionPointer(CallingConvention.StdCall, CharSet = CharSet.Ansi)]
    delegate short ClientConnectFn(IntPtr hwnd, short deviceId, string protocol, int txBufferSize, int rxBufferSize, short isAppPacketizing);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate short ClientDisconnectFn(short clientId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate short ReadMessageFn(short clientId, byte[] buffer, short bufferSize, short blockOnRead);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate short SendCommandFn(short command, short clientId, byte[] data, short size);

    [UnmanagedFunctionPointer(CallingConvention.StdCall, CharSet = CharSet.Ansi)]
    delegate short GetErrorMsgFn(short code, StringBuilder description);

    class Rp1210Reader : ICanReader
    {
        const short SetAllFiltersToPass = 3, EchoTransmittedMessages = 16, SetMessageReceive = 18,
            GetProtocolConnectionSpeed = 45;

        IntPtr dll;
        short client = -1;
        readonly ClientDisconnectFn disconnect;
        readonly ReadMessageFn read;
        readonly GetErrorMsgFn errorText;
        readonly byte[] buffer = new byte[512];
        readonly string description;
        bool idOnlyLayout;

        public string Description { get { return description; } }

        public Rp1210Reader(string api, short deviceId, IList<string> protocols)
        {
            dll = Native.Load(api.EndsWith(".dll", StringComparison.OrdinalIgnoreCase) ? api : api + ".dll");
            var connect = Native.Fn<ClientConnectFn>(dll, "RP1210_ClientConnect", true);
            disconnect = Native.Fn<ClientDisconnectFn>(dll, "RP1210_ClientDisconnect", true);
            read = Native.Fn<ReadMessageFn>(dll, "RP1210_ReadMessage", true);
            var command = Native.Fn<SendCommandFn>(dll, "RP1210_SendCommand", true);
            errorText = Native.Fn<GetErrorMsgFn>(dll, "RP1210_GetErrorMsg", false);

            var failures = new List<string>();
            string used = null;
            foreach (string p in protocols)
            {
                short r = connect(IntPtr.Zero, deviceId, p, 8192, 65536, 0);
                if (r >= 0 && r < 128) { client = r; used = p; break; }
                failures.Add(string.Format("\"{0}\": {1}", p, Describe(r)));
            }
            if (client < 0) throw new AdapterException("Could not connect: " + string.Join("; ", failures.ToArray()));

            short pass = command(SetAllFiltersToPass, client, new byte[0], 0);
            if (pass != 0) throw new AdapterException("The adapter refused to pass all messages: " + Describe(pass));
            command(EchoTransmittedMessages, client, new byte[] { 0 }, 1);
            command(SetMessageReceive, client, new byte[] { 1 }, 1);
            description = string.Format("RP1210 {0} device {1}, \"{2}\"", api, deviceId, used);
            var speed = new byte[17];
            if (command(GetProtocolConnectionSpeed, client, speed, (short)speed.Length) == 0)
            {
                string s = Encoding.ASCII.GetString(speed).TrimEnd('\0', ' ');
                if (s.Length > 0) description += ", bus speed " + s;
            }
        }

        string Describe(short code)
        {
            if (code < 0) code = (short)-code;
            if (errorText != null)
            {
                var sb = new StringBuilder(256);
                try { if (errorText(code, sb) == 0 && sb.Length > 0) return "error " + code + " " + sb; }
                catch (Exception) { }
            }
            return "error " + code;
        }

        public int Read(Output output)
        {
            int count = 0;
            while (count < 2000)
            {
                short n = read(client, buffer, (short)buffer.Length, 0);
                if (n < 0) throw new AdapterException("Read failed: " + Describe(n));
                if (n == 0) break;
                if (Decode(n, output)) count++;
            }
            return count;
        }

        /// <summary>
        /// RP1210 CAN receive messages: [timestamp x4][CAN type: 0 std, 1 ext][id x2 or x4][data].
        /// Some drivers put a 4-byte id with no type byte instead (as the NEXIQ driver's own debug log
        /// shows); once a message only fits that layout, the rest of the session is read that way.
        /// </summary>
        bool Decode(int n, Output output)
        {
            byte[] b = buffer;
            if (n < 7) return false;
            uint micros = BigEndian(b, 0, 4);
            if (!idOnlyLayout)
            {
                if (b[4] == 1 && n >= 9 && n - 9 <= 8)
                {
                    output.Frame(micros, BigEndian(b, 5, 4) & 0x1FFFFFFF, true, b, 9, n - 9);
                    return true;
                }
                if (b[4] == 0 && n - 7 <= 8)
                {
                    output.Frame(micros, BigEndian(b, 5, 2) & 0x7FF, false, b, 7, n - 7);
                    return true;
                }
                if (n >= 8 && n - 8 <= 8) idOnlyLayout = true; // e.g. 16 bytes: 4 time + 4 id + 8 data
                else return false;
            }
            if (n < 8 || n - 8 > 8) return false;
            uint id = BigEndian(b, 4, 4) & 0x1FFFFFFF;
            output.Frame(micros, id, id > 0x7FF, b, 8, n - 8);
            return true;
        }

        static uint BigEndian(byte[] b, int start, int count)
        {
            uint v = 0;
            for (int i = 0; i < count; i++) v = (v << 8) | b[start + i];
            return v;
        }

        public void Dispose()
        {
            if (client >= 0) { try { disconnect(client); } catch (Exception) { } client = -1; }
            if (dll != IntPtr.Zero) { Kernel32.FreeLibrary(dll); dll = IntPtr.Zero; }
        }
    }

    // ---------------------------------------------------------------- J2534 (SAE J2534-1, 04.04)

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruOpenFn(IntPtr name, out uint deviceId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruCloseFn(uint deviceId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruConnectFn(uint deviceId, uint protocolId, uint flags, uint baud, out uint channelId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruDisconnectFn(uint channelId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruReadMsgsFn(uint channelId, IntPtr msgs, ref uint numMsgs, uint timeout);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate int PassThruStartMsgFilterFn(uint channelId, uint filterType, IntPtr mask, IntPtr pattern, IntPtr flowControl, out uint filterId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall, CharSet = CharSet.Ansi)]
    delegate int PassThruGetLastErrorFn(StringBuilder description);

    [UnmanagedFunctionPointer(CallingConvention.StdCall, CharSet = CharSet.Ansi)]
    delegate int PassThruReadVersionFn(uint deviceId, StringBuilder firmware, StringBuilder dll, StringBuilder api);

    class J2534Reader : ICanReader
    {
        const uint CAN = 5, PASS_FILTER = 1, CAN_29BIT_ID = 0x100, CAN_ID_BOTH = 0x800;
        const uint TX_MSG_TYPE = 0x01, TX_DONE = 0x08;
        const int ERR_TIMEOUT = 0x09, ERR_BUFFER_EMPTY = 0x10;
        // PASSTHRU_MSG: ProtocolID, RxStatus, TxFlags, Timestamp, DataSize, ExtraDataIndex, Data[4128]
        const int MsgSize = 24 + 4128, Batch = 64;

        IntPtr dll, msgs;
        uint device, channel;
        bool opened, connected;
        readonly PassThruCloseFn close;
        readonly PassThruDisconnectFn disconnect;
        readonly PassThruReadMsgsFn readMsgs;
        readonly PassThruGetLastErrorFn lastError;
        readonly byte[] data = new byte[MsgSize];
        readonly string description;

        public string Description { get { return description; } }

        public J2534Reader(string dllPath, uint kbps)
        {
            dll = Native.Load(dllPath);
            var open = Native.Fn<PassThruOpenFn>(dll, "PassThruOpen", true);
            close = Native.Fn<PassThruCloseFn>(dll, "PassThruClose", true);
            var connect = Native.Fn<PassThruConnectFn>(dll, "PassThruConnect", true);
            disconnect = Native.Fn<PassThruDisconnectFn>(dll, "PassThruDisconnect", true);
            readMsgs = Native.Fn<PassThruReadMsgsFn>(dll, "PassThruReadMsgs", true);
            var startFilter = Native.Fn<PassThruStartMsgFilterFn>(dll, "PassThruStartMsgFilter", true);
            lastError = Native.Fn<PassThruGetLastErrorFn>(dll, "PassThruGetLastError", false);
            var readVersion = Native.Fn<PassThruReadVersionFn>(dll, "PassThruReadVersion", false);

            Check(open(IntPtr.Zero, out device), "PassThruOpen (is the adapter plugged in, and no other program using it?)");
            opened = true;
            // Both 11- and 29-bit ids where the adapter allows it, else 11-bit only
            int r = connect(device, CAN, CAN_ID_BOTH, kbps * 1000, out channel);
            string ids = "11- and 29-bit ids";
            if (r != 0) { r = connect(device, CAN, 0, kbps * 1000, out channel); ids = "11-bit ids"; }
            Check(r, string.Format("PassThruConnect at {0} kbit/s", kbps));
            connected = true;

            msgs = Marshal.AllocHGlobal(MsgSize * Batch);
            IntPtr mask = Marshal.AllocHGlobal(MsgSize), pattern = Marshal.AllocHGlobal(MsgSize);
            try
            {
                // A pass filter whose mask is all zeros lets every id through
                foreach (IntPtr m in new[] { mask, pattern })
                {
                    Marshal.Copy(new byte[MsgSize], 0, m, MsgSize);
                    Marshal.WriteInt32(m, 0, (int)CAN);
                    Marshal.WriteInt32(m, 16, 4);
                }
                uint filterId;
                Check(startFilter(channel, PASS_FILTER, mask, pattern, IntPtr.Zero, out filterId), "PassThruStartMsgFilter");
            }
            finally
            {
                Marshal.FreeHGlobal(mask);
                Marshal.FreeHGlobal(pattern);
            }

            description = string.Format("J2534 {0}, {1} kbit/s, {2}", Path.GetFileName(dllPath), kbps, ids);
            if (readVersion != null)
            {
                var fw = new StringBuilder(80); var dv = new StringBuilder(80); var api = new StringBuilder(80);
                try
                {
                    if (readVersion(device, fw, dv, api) == 0)
                        description += string.Format(", firmware {0}, driver {1}", fw.ToString().Trim(), dv.ToString().Trim());
                }
                catch (Exception) { }
            }
        }

        void Check(int code, string what)
        {
            if (code == 0) return;
            string text = "";
            if (lastError != null)
            {
                var sb = new StringBuilder(80);
                try { if (lastError(sb) == 0) text = sb.ToString().Trim(); } catch (Exception) { }
            }
            throw new AdapterException(string.Format("{0} failed: error 0x{1:X2}{2}", what, code, text.Length > 0 ? " " + text : ""));
        }

        public int Read(Output output)
        {
            uint n = Batch;
            int r = readMsgs(channel, msgs, ref n, 0);
            if (r != 0 && r != ERR_BUFFER_EMPTY && r != ERR_TIMEOUT) Check(r, "PassThruReadMsgs");
            int count = 0;
            for (int i = 0; i < n && i < Batch; i++)
            {
                IntPtr m = new IntPtr(msgs.ToInt64() + (long)i * MsgSize);
                uint rx = (uint)Marshal.ReadInt32(m, 4);
                uint micros = (uint)Marshal.ReadInt32(m, 12);
                int size = Marshal.ReadInt32(m, 16);
                if ((rx & (TX_MSG_TYPE | TX_DONE)) != 0 || size < 4 || size > 4 + 64) continue;
                Marshal.Copy(new IntPtr(m.ToInt64() + 24), data, 0, size);
                uint id = (uint)(data[0] << 24 | data[1] << 16 | data[2] << 8 | data[3]);
                bool extended = (rx & CAN_29BIT_ID) != 0;
                output.Frame(micros, id & (extended ? 0x1FFFFFFFu : 0x7FFu), extended, data, 4, size - 4);
                count++;
            }
            return count;
        }

        public void Dispose()
        {
            if (connected) { try { disconnect(channel); } catch (Exception) { } connected = false; }
            if (opened) { try { close(device); } catch (Exception) { } opened = false; }
            if (msgs != IntPtr.Zero) { Marshal.FreeHGlobal(msgs); msgs = IntPtr.Zero; }
            if (dll != IntPtr.Zero) { Kernel32.FreeLibrary(dll); dll = IntPtr.Zero; }
        }
    }

    // ---------------------------------------------------------------- main

    static class Program
    {
        static volatile bool stop;

        static int Main(string[] args)
        {
            var output = new Output(Console.OpenStandardOutput());
            if (args.Length == 0)
            {
                Console.Error.WriteLine("Run by PandaCapture: pandacapture --adapter ... (see pandacapture list)");
                return 2;
            }
            // Closing stdin (or PandaCapture exiting) stops the reader
            var watcher = new Thread(() =>
            {
                try { Console.OpenStandardInput().CopyTo(Stream.Null); } catch (Exception) { }
                stop = true;
            });
            watcher.IsBackground = true;
            watcher.Start();

            ICanReader reader = null;
            try
            {
                if (args[0] == "rp1210" && args.Length >= 4)
                {
                    var protocols = new List<string>();
                    for (int i = 3; i < args.Length; i++) protocols.Add(args[i]);
                    reader = new Rp1210Reader(args[1], short.Parse(args[2]), protocols);
                }
                else if (args[0] == "j2534" && args.Length == 3)
                    reader = new J2534Reader(args[1], uint.Parse(args[2]));
                else
                    throw new AdapterException("bad arguments: " + string.Join(" ", args));

                output.Text('I', reader.Description + (Environment.Is64BitProcess ? " (64-bit driver)" : " (32-bit driver)"));
                while (!stop)
                {
                    int n = reader.Read(output);
                    output.Flush();
                    if (n == 0) Thread.Sleep(1);
                }
                return 0;
            }
            catch (Exception e)
            {
                try { output.Flush(); output.Text('E', e is AdapterException ? e.Message : e.GetType().Name + ": " + e.Message); }
                catch (Exception) { }
                return 1;
            }
            finally
            {
                if (reader != null) reader.Dispose();
            }
        }
    }
}
