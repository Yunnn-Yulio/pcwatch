using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Management;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace PCWatch
{
    internal static class Program
    {
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        internal static string DataRoot;
        internal static string StartupLink { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Startup), "PCWatch.lnk"); } }
        internal static string SignalName;
        internal static string MutexName;
        internal static int Interval = 15, DetailInterval = 60, MaxSeconds = 0;
        internal static long MaxLogBytes = 100L * 1024 * 1024, SegmentBytes = 5L * 1024 * 1024;
        internal static int RetentionDays = 14;

        [STAThread]
        private static void Main(string[] args)
        {
            try
            {
                DataRoot = Path.GetFullPath(Value(args, "--data-dir", Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PCWatch")));
                string token;
                using (SHA256 hash = SHA256.Create()) { token = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(DataRoot.ToUpperInvariant()))).Replace("-", "").Substring(0, 24); }
                SignalName = "Local\\PCWatchStop-" + token;
                MutexName = "Local\\PCWatchRun-" + token;
                if (args.Contains("--stop")) { Stop(); return; }
                if (args.Contains("--remove-startup")) { RemoveStartup(); Stop(); return; }
                if (args.Contains("--install-startup")) { InstallStartup(); return; }
                Interval = Number(args, "--interval", 15, 2, 300);
                DetailInterval = Number(args, "--details", 60, Interval, 3600);
                MaxSeconds = Number(args, "--max-seconds", 0, 0, 86400);
                MaxLogBytes = Number(args, "--max-log-mb", 100, 1, 1024) * 1024L * 1024;
                SegmentBytes = Math.Min(Number(args, "--segment-kb", 5120, 16, 65536) * 1024L, MaxLogBytes / 4);
                RetentionDays = Number(args, "--retention-days", 14, 1, 365);
                if (args.Contains("--cleanup-only")) { new LogStore(DataRoot).Cleanup(); return; }
                bool acquired;
                using (Mutex mutex = new Mutex(true, MutexName, out acquired))
                {
                    if (!acquired) return;
                    using (EventWaitHandle stop = new EventWaitHandle(false, EventResetMode.ManualReset, SignalName))
                    {
                        stop.Reset();
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        using (MonitorContext context = new MonitorContext(stop, !args.Contains("--no-tray"))) { Application.Run(context); }
                    }
                    mutex.ReleaseMutex();
                }
            }
            catch (Exception ex)
            {
                Environment.ExitCode = 1;
                try
                {
                    if (!String.IsNullOrEmpty(DataRoot))
                    {
                        Directory.CreateDirectory(DataRoot);
                        AtomicJson(Path.Combine(DataRoot, "last-error.json"), new Dictionary<string, object> { { "utc", Utc() }, { "error_type", ex.GetType().Name }, { "error_code", ex.HResult } });
                    }
                }
                catch { }
            }
        }
        internal static string Value(string[] args, string key, string fallback)
        {
            int i = Array.IndexOf(args, key);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : fallback;
        }
        internal static int Number(string[] args, string key, int fallback, int minimum, int maximum)
        {
            int value;
            return Int32.TryParse(Value(args, key, fallback.ToString(CultureInfo.InvariantCulture)), out value) && value >= minimum && value <= maximum ? value : fallback;
        }
        internal static string Utc() { return DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture); }
        internal static string Serialize(object value) { lock (Json) { return Json.Serialize(value); } }
        internal static void AtomicJson(string path, object value)
        {
            string temp = path + ".tmp";
            File.WriteAllText(temp, Serialize(value), new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temp, path, null); else File.Move(temp, path);
        }
        internal static void Stop()
        {
            try { using (EventWaitHandle handle = EventWaitHandle.OpenExisting(SignalName)) { handle.Set(); } }
            catch (WaitHandleCannotBeOpenedException) { }
        }
        internal static void InstallStartup()
        {
            string executable = Assembly.GetExecutingAssembly().Location;
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            object shell = Activator.CreateInstance(shellType);
            object link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { StartupLink });
            try
            {
                Type type = link.GetType();
                if (File.Exists(StartupLink))
                {
                    string existingTarget = Convert.ToString(type.InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null));
                    if (String.IsNullOrEmpty(existingTarget) || !String.Equals(Path.GetFullPath(existingTarget), Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase)) throw new IOException("An unrelated startup shortcut already has this name.");
                }
                type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { executable });
                type.InvokeMember("Arguments", BindingFlags.SetProperty, null, link, new object[] { "--data-dir \"" + DataRoot + "\"" });
                type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { Path.GetDirectoryName(executable) });
                type.InvokeMember("Description", BindingFlags.SetProperty, null, link, new object[] { "PCWatch: monitor local ligero de recursos y errores" });
                type.InvokeMember("WindowStyle", BindingFlags.SetProperty, null, link, new object[] { 7 });
                type.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
            }
            finally { Marshal.FinalReleaseComObject(link); Marshal.FinalReleaseComObject(shell); }
        }
        internal static void RemoveStartup()
        {
            if (!File.Exists(StartupLink)) return;
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            object shell = Activator.CreateInstance(shellType);
            object link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { StartupLink });
            try
            {
                string target = Convert.ToString(link.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null));
                if (String.Equals(Path.GetFullPath(target), Path.GetFullPath(Assembly.GetExecutingAssembly().Location), StringComparison.OrdinalIgnoreCase)) File.Delete(StartupLink);
            }
            finally { Marshal.FinalReleaseComObject(link); Marshal.FinalReleaseComObject(shell); }
        }
    }

    internal sealed class LogStore
    {
        private readonly string root;
        private readonly object gate = new object();
        private static readonly Regex OwnFile = new Regex(@"^(resources|processes|events|activity)-\d{8}-\d{3,6}\.(csv|jsonl)$", RegexOptions.CultureInvariant);
        internal LogStore(string dataRoot)
        {
            root = Path.GetFullPath(Path.Combine(dataRoot, "Logs"));
            Directory.CreateDirectory(root);
            if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("The log directory must not be a link.");
        }
        internal void JsonLine(string kind, IDictionary<string, object> record)
        {
            string text;
            lock (gate) { text = Program.Serialize(record); Append(kind, ".jsonl", text + Environment.NewLine, null); }
        }
        internal void Sample(IDictionary<string, object> record)
        {
            lock (gate)
            {
                string header = String.Join(",", record.Keys);
                string line = String.Join(",", record.Values.Select(v => v == null ? "" : Convert.ToString(v, CultureInfo.InvariantCulture)));
                Append("resources", ".csv", line + Environment.NewLine, header);
            }
        }
        private void Append(string kind, string extension, string text, string header)
        {
            string day = DateTime.UtcNow.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
            int segment = 1;
            string path;
            while (true)
            {
                path = Path.Combine(root, kind + "-" + day + "-" + segment.ToString("000", CultureInfo.InvariantCulture) + extension);
                if (!File.Exists(path) || new FileInfo(path).Length < Program.SegmentBytes) break;
                segment++;
            }
            bool fresh = !File.Exists(path) || new FileInfo(path).Length == 0;
            using (FileStream stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read))
            using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
            {
                if (fresh && header != null) writer.WriteLine(header);
                writer.Write(text);
                writer.Flush();
                stream.Flush(true);
            }
        }
        internal void Cleanup()
        {
            lock (gate)
            {
                DateTime cutoff = DateTime.UtcNow.AddDays(-Program.RetentionDays);
                List<FileInfo> files = new DirectoryInfo(root).GetFiles().Where(f => OwnFile.IsMatch(f.Name) && (f.Attributes & FileAttributes.ReparsePoint) == 0).OrderBy(f => f.LastWriteTimeUtc).ToList();
                foreach (FileInfo file in files.Where(f => f.LastWriteTimeUtc < cutoff).ToList()) SafeDelete(file);
                files = files.Where(f => File.Exists(f.FullName)).ToList();
                long total = files.Sum(f => new FileInfo(f.FullName).Length);
                foreach (FileInfo file in files)
                {
                    if (total <= Program.MaxLogBytes) break;
                    long bytes = file.Length;
                    if (SafeDelete(file)) total -= bytes;
                }
            }
        }
        private bool SafeDelete(FileInfo file)
        {
            string resolved = Path.GetFullPath(file.FullName);
            if (!String.Equals(Path.GetDirectoryName(resolved), root, StringComparison.OrdinalIgnoreCase) || !OwnFile.IsMatch(file.Name)) return false;
            try { file.Delete(); return true; } catch (IOException) { return false; } catch (UnauthorizedAccessException) { return false; }
        }
    }

    internal static class Native
    {
        [StructLayout(LayoutKind.Sequential)] internal struct MemoryStatus
        {
            public uint Length, MemoryLoad;
            public ulong TotalPhysical, AvailablePhysical, TotalPageFile, AvailablePageFile, TotalVirtual, AvailableVirtual, AvailableExtendedVirtual;
        }
        [StructLayout(LayoutKind.Sequential)] internal struct PerformanceInfo
        {
            public uint Size;
            public UIntPtr CommitTotal, CommitLimit, CommitPeak, PhysicalTotal, PhysicalAvailable, SystemCache, KernelTotal, KernelPaged, KernelNonpaged, PageSize;
            public uint HandleCount, ProcessCount, ThreadCount;
        }
        [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool GlobalMemoryStatusEx(ref MemoryStatus value);
        [DllImport("kernel32.dll", SetLastError = true)] internal static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
        [DllImport("psapi.dll", SetLastError = true)] internal static extern bool GetPerformanceInfo(ref PerformanceInfo value, uint size);
        [DllImport("user32.dll", SetLastError = true)] internal static extern IntPtr RegisterPowerSettingNotification(IntPtr handle, ref Guid setting, uint flags);
        [DllImport("user32.dll")] internal static extern bool UnregisterPowerSettingNotification(IntPtr handle);
        internal delegate bool EnumWindowCallback(IntPtr hwnd, IntPtr param);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumWindowCallback callback, IntPtr param);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern bool IsHungAppWindow(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    }

    internal sealed class Counters : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)] private struct CounterValue { public uint Status; public double Value; }
        [StructLayout(LayoutKind.Sequential)] private struct CounterItem { public IntPtr Name; public CounterValue Value; }
        [DllImport("pdh.dll", CharSet = CharSet.Unicode)] private static extern uint PdhOpenQuery(string source, IntPtr data, out IntPtr query);
        [DllImport("pdh.dll", CharSet = CharSet.Unicode)] private static extern uint PdhAddEnglishCounter(IntPtr query, string path, IntPtr data, out IntPtr counter);
        [DllImport("pdh.dll")] private static extern uint PdhCollectQueryData(IntPtr query);
        [DllImport("pdh.dll")] private static extern uint PdhCloseQuery(IntPtr query);
        [DllImport("pdh.dll")] private static extern uint PdhGetFormattedCounterValue(IntPtr counter, uint format, out uint type, out CounterValue value);
        [DllImport("pdh.dll", CharSet = CharSet.Unicode, EntryPoint = "PdhGetFormattedCounterArrayW")] private static extern uint PdhGetFormattedCounterArray(IntPtr counter, uint format, ref uint bytes, ref uint count, IntPtr buffer);
        private IntPtr query;
        private readonly Dictionary<string, IntPtr> handles = new Dictionary<string, IntPtr>();
        internal readonly List<string> Missing = new List<string>();
        internal Counters()
        {
            if (PdhOpenQuery(null, IntPtr.Zero, out query) != 0) { Missing.Add("pdh_query"); return; }
            Add("disk_queue", @"\PhysicalDisk(_Total)\Current Disk Queue Length");
            Add("disk_read_ms", @"\PhysicalDisk(_Total)\Avg. Disk sec/Read");
            Add("disk_write_ms", @"\PhysicalDisk(_Total)\Avg. Disk sec/Write");
            Add("disk_read_Bps", @"\PhysicalDisk(_Total)\Disk Read Bytes/sec");
            Add("disk_write_Bps", @"\PhysicalDisk(_Total)\Disk Write Bytes/sec");
            Add("pages_input_per_sec", @"\Memory\Pages Input/sec");
            Add("pages_output_per_sec", @"\Memory\Pages Output/sec");
            Add("gpu_dedicated_MiB", @"\GPU Adapter Memory(*)\Dedicated Usage");
            Add("gpu_shared_MiB", @"\GPU Adapter Memory(*)\Shared Usage");
            Collect();
        }
        private void Add(string key, string path)
        {
            IntPtr counter;
            if (PdhAddEnglishCounter(query, path, IntPtr.Zero, out counter) == 0) handles.Add(key, counter); else Missing.Add(key);
        }
        internal void Collect() { if (query != IntPtr.Zero) PdhCollectQueryData(query); }
        internal object Read(string key, double scale)
        {
            IntPtr handle;
            uint type;
            CounterValue value;
            if (!handles.TryGetValue(key, out handle) || PdhGetFormattedCounterValue(handle, 0x200, out type, out value) != 0 || value.Status > 1 || Double.IsNaN(value.Value) || Double.IsInfinity(value.Value)) return null;
            return Math.Round(value.Value * scale, 3);
        }
        internal object ReadArraySum(string key)
        {
            IntPtr handle;
            if (!handles.TryGetValue(key, out handle)) return null;
            uint bytes = 0, count = 0;
            uint result = PdhGetFormattedCounterArray(handle, 0x200, ref bytes, ref count, IntPtr.Zero);
            if (result != 0x800007D2 || bytes == 0 || bytes > 1024 * 1024) return null;
            IntPtr buffer = Marshal.AllocHGlobal((int)bytes);
            try
            {
                if (PdhGetFormattedCounterArray(handle, 0x200, ref bytes, ref count, buffer) != 0) return null;
                double sum = 0;
                int valid = 0, size = Marshal.SizeOf(typeof(CounterItem));
                for (int i = 0; i < count; i++)
                {
                    CounterItem item = (CounterItem)Marshal.PtrToStructure(IntPtr.Add(buffer, i * size), typeof(CounterItem));
                    if (item.Value.Status <= 1 && !Double.IsNaN(item.Value.Value) && !Double.IsInfinity(item.Value.Value)) { sum += item.Value.Value; valid++; }
                }
                return valid > 0 ? (object)Math.Round(sum / 1048576.0, 2) : null;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        public void Dispose() { if (query != IntPtr.Zero) { PdhCloseQuery(query); query = IntPtr.Zero; } }
    }

    internal sealed class PowerWindow : Form
    {
        private readonly Action<IDictionary<string, object>> emit;
        private IntPtr subscription;
        internal PowerWindow(Action<IDictionary<string, object>> callback)
        {
            emit = callback;
            ShowInTaskbar = false;
            Guid setting = new Guid("6fe69556-704a-47a0-8f24-c28d936fda47");
            subscription = Native.RegisterPowerSettingNotification(Handle, ref setting, 0);
            if (subscription == IntPtr.Zero) emit(new Dictionary<string, object> { { "type", "power_notification_unavailable" }, { "utc", Program.Utc() }, { "error_code", Marshal.GetLastWin32Error() } });
        }
        protected override void SetVisibleCore(bool value) { base.SetVisibleCore(false); }
        protected override void WndProc(ref Message message)
        {
            if (message.Msg == 0x218)
            {
                int code = message.WParam.ToInt32();
                string kind = code == 4 ? "suspend" : code == 7 ? "resume_user" : code == 18 ? "resume_automatic" : code == 6 ? "resume_critical" : null;
                if (kind != null) emit(new Dictionary<string, object> { { "type", "power" }, { "state", kind }, { "utc", Program.Utc() } });
                if (code == 0x8013 && message.LParam != IntPtr.Zero)
                {
                    Guid setting = (Guid)Marshal.PtrToStructure(message.LParam, typeof(Guid));
                    if (setting == new Guid("6fe69556-704a-47a0-8f24-c28d936fda47") && Marshal.ReadInt32(message.LParam, 16) == 4)
                    {
                        int state = Marshal.ReadInt32(message.LParam, 20);
                        emit(new Dictionary<string, object> { { "type", "display" }, { "state", state == 0 ? "off" : state == 1 ? "on" : state == 2 ? "dimmed" : "unknown" }, { "utc", Program.Utc() } });
                    }
                }
            }
            base.WndProc(ref message);
        }
        protected override void Dispose(bool disposing)
        {
            if (subscription != IntPtr.Zero) { Native.UnregisterPowerSettingNotification(subscription); subscription = IntPtr.Zero; }
            base.Dispose(disposing);
        }
    }

    internal sealed class MonitorContext : ApplicationContext, IDisposable
    {
        private readonly EventWaitHandle stop;
        private readonly LogStore logs;
        private readonly PowerWindow window;
        private readonly NotifyIcon tray;
        private readonly Thread worker;
        private readonly System.Windows.Forms.Timer exitTimer;
        private readonly object statusGate = new object();
        private Dictionary<string, object> latest;
        private readonly Dictionary<string, Dictionary<string, object>> logErrors = new Dictionary<string, Dictionary<string, object>>();
        private string lastSuccessfulSampleUtc;
        private readonly Dictionary<int, Tuple<long, double>> processCpu = new Dictionary<int, Tuple<long, double>>();
        private readonly Stopwatch uptime = Stopwatch.StartNew();
        private readonly HashSet<string> errors = new HashSet<string>();
        private volatile bool finished;
        private bool disposed;
        private long lastIdle, lastTotal;
        private double lastSampleSeconds, lastSelfCpu, lastDetails, lastProcessSeconds;
        private DateTime lastSampleUtc;
        internal MonitorContext(EventWaitHandle stopHandle, bool showTray)
        {
            stop = stopHandle;
            logs = new LogStore(Program.DataRoot);
            logs.Cleanup();
            try
            {
                string statusFile = Path.Combine(Program.DataRoot, "status.json");
                if (File.Exists(statusFile) && new FileInfo(statusFile).Length < 65536)
                {
                    Dictionary<string, object> previous = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(statusFile));
                    if (previous.ContainsKey("running") && Convert.ToBoolean(previous["running"])) Emit("activity", new Dictionary<string, object> { { "type", "previous_monitor_session_without_stop" }, { "utc", Program.Utc() }, { "last_sample_utc", previous.ContainsKey("last_sample_utc") ? previous["last_sample_utc"] : previous.ContainsKey("utc") ? previous["utc"] : null }, { "interpretation", "previous_session_end_cause_unknown" } });
                }
            }
            catch { }
            Emit("activity", new Dictionary<string, object> { { "type", "start" }, { "utc", Program.Utc() }, { "pid", Process.GetCurrentProcess().Id }, { "interval_seconds", Program.Interval }, { "details_seconds", Program.DetailInterval }, { "max_log_mib", Program.MaxLogBytes / 1048576 }, { "retention_days", Program.RetentionDays } });
            window = new PowerWindow(record => Emit("activity", record));
            SystemEvents.SessionSwitch += SessionChanged;
            if (showTray)
            {
                ContextMenuStrip menu = new ContextMenuStrip();
                menu.Items.Add("Estado", null, (sender, args) => ShowStatus());
                menu.Items.Add("Abrir registros", null, (sender, args) => Process.Start(new ProcessStartInfo(Path.Combine(Program.DataRoot, "Logs")) { UseShellExecute = true }));
                menu.Items.Add("Marcar incidente ahora", null, (sender, args) => Emit("activity", new Dictionary<string, object> { { "type", "user_mark" }, { "utc", Program.Utc() } }));
                menu.Items.Add("Quitar inicio automatico y detener", null, (sender, args) => { Program.RemoveStartup(); stop.Set(); });
                menu.Items.Add("Detener monitor", null, (sender, args) => stop.Set());
                tray = new NotifyIcon { Icon = SystemIcons.Information, Text = "PCWatch: registros cada " + Program.Interval + " segundos", ContextMenuStrip = menu, Visible = true };
                tray.DoubleClick += (sender, args) => ShowStatus();
            }
            exitTimer = new System.Windows.Forms.Timer { Interval = 1000 };
            exitTimer.Tick += (sender, args) => { UpdateTrayHealth(); if (finished) ExitThread(); };
            exitTimer.Start();
            worker = new Thread(Run) { IsBackground = true, Name = "PCWatch sampler" };
            worker.Start();
        }
        private void SessionChanged(object sender, SessionSwitchEventArgs args)
        {
            if (args.Reason == SessionSwitchReason.SessionLock || args.Reason == SessionSwitchReason.SessionUnlock) Emit("activity", new Dictionary<string, object> { { "type", "session" }, { "state", args.Reason == SessionSwitchReason.SessionLock ? "locked" : "unlocked" }, { "utc", Program.Utc() } });
        }
        private void ShowStatus()
        {
            Dictionary<string, object> snapshot;
            string savedUtc;
            string[] failedComponents;
            lock (statusGate) { snapshot = latest; savedUtc = lastSuccessfulSampleUtc; failedComponents = logErrors.Keys.OrderBy(k => k).ToArray(); }
            string text = failedComponents.Length == 0 ? "Monitor activo.\n" : "ATENCION: fallo al guardar registros o estado.\nComponentes: " + String.Join(", ", failedComponents) + ".\n";
            text += "Registros locales cada " + Program.Interval + " segundos.\nUltima muestra guardada (UTC): " + (savedUtc ?? "ninguna en esta sesion") + "\n\n";
            if (snapshot != null) text += "RAM usada: " + snapshot["ram_used_MiB"] + " MiB\nRAM disponible: " + snapshot["ram_available_MiB"] + " MiB\nCPU total: " + snapshot["cpu_pct"] + "%\nRAM del monitor: " + snapshot["monitor_working_MiB"] + " MiB\n\n";
            text += "Limite: " + Program.MaxLogBytes / 1048576 + " MiB; retencion: " + Program.RetentionDays + " dias.\nCarpeta: " + Path.Combine(Program.DataRoot, "Logs");
            MessageBox.Show(text, "PCWatch", MessageBoxButtons.OK, failedComponents.Length == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }
        private void UpdateTrayHealth()
        {
            if (tray == null) return;
            bool healthy;
            lock (statusGate) { healthy = logErrors.Count == 0; }
            string text = healthy ? "PCWatch: registros cada " + Program.Interval + " segundos" : "PCWatch: fallo al guardar registros";
            if (tray.Text != text) { tray.Text = text; tray.Icon = healthy ? SystemIcons.Information : SystemIcons.Warning; }
        }
        private void SetLogError(string component, Exception error)
        {
            lock (statusGate)
            {
                if (error == null) logErrors.Remove(component);
                else logErrors[component] = new Dictionary<string, object> { { "error_type", error.GetType().Name }, { "error_code", error.HResult } };
            }
        }
        private void WriteJsonLog(string kind, IDictionary<string, object> record)
        {
            try { logs.JsonLine(kind, record); SetLogError(kind, null); }
            catch (Exception ex) { SetLogError(kind, ex); throw; }
        }
        private void SaveStatus(bool running)
        {
            // Serialize status writes from both threads. A successful status write only
            // clears its own error; failures in other log streams remain visible.
            lock (statusGate)
            {
                Dictionary<string, object> status = latest == null ? new Dictionary<string, object>() : new Dictionary<string, object>(latest);
                Dictionary<string, Dictionary<string, object>> remainingErrors = logErrors.Where(p => p.Key != "status").ToDictionary(p => p.Key, p => p.Value);
                if (!status.ContainsKey("utc")) status["utc"] = Program.Utc();
                status["running"] = running;
                status["startup_enabled"] = File.Exists(Program.StartupLink);
                status["last_sample_utc"] = lastSuccessfulSampleUtc;
                status["logging_healthy"] = remainingErrors.Count == 0;
                status["log_error_components"] = remainingErrors.Keys.OrderBy(k => k).ToArray();
                status["log_errors"] = remainingErrors;
                try { Program.AtomicJson(Path.Combine(Program.DataRoot, "status.json"), status); logErrors.Remove("status"); }
                catch (Exception ex) { logErrors["status"] = new Dictionary<string, object> { { "error_type", ex.GetType().Name }, { "error_code", ex.HResult } }; }
            }
        }
        private void Emit(string kind, IDictionary<string, object> record)
        {
            try { WriteJsonLog(kind, record); } catch { }
        }
        private void ErrorOnce(string component, Exception ex)
        {
            string key = component + ":" + ex.GetType().Name + ":" + ex.HResult;
            if (errors.Add(key)) Emit("activity", new Dictionary<string, object> { { "type", "collection_error" }, { "component", component }, { "utc", Program.Utc() }, { "error_type", ex.GetType().Name }, { "error_code", ex.HResult } });
        }
        private void Baseline()
        {
            Dictionary<string, object> record = new Dictionary<string, object> { { "type", "baseline" }, { "utc", Program.Utc() }, { "logical_processors", Environment.ProcessorCount }, { "monitor_version", "0.1.0" }, { "time_zone", TimeZoneInfo.Local.Id } };
            string[] queries = { "SELECT Caption,Version,BuildNumber FROM Win32_OperatingSystem", "SELECT Product FROM Win32_BaseBoard", "SELECT SMBIOSBIOSVersion FROM Win32_BIOS", "SELECT Name FROM Win32_Processor", "SELECT Capacity,ConfiguredClockSpeed,PartNumber FROM Win32_PhysicalMemory", "SELECT Name,DriverVersion FROM Win32_VideoController" };
            string[] keys = { "operating_system", "motherboard", "bios", "cpu", "ram_modules", "graphics" };
            for (int i = 0; i < queries.Length; i++)
            {
                try
                {
                    List<Dictionary<string, object>> values = new List<Dictionary<string, object>>();
                    using (ManagementObjectSearcher search = new ManagementObjectSearcher(queries[i]))
                    {
                        search.Options.Timeout = TimeSpan.FromSeconds(5);
                        using (ManagementObjectCollection results = search.Get())
                        foreach (ManagementObject item in results)
                        {
                            using (item)
                            {
                                Dictionary<string, object> value = new Dictionary<string, object>();
                                foreach (PropertyData property in item.Properties) value[property.Name] = property.Value;
                                values.Add(value);
                            }
                        }
                    }
                    record[keys[i]] = values;
                }
                catch (Exception ex) { ErrorOnce("baseline_" + keys[i], ex); }
            }
            Emit("activity", record);
        }
        private void Run()
        {
            try
            {
                try { Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.BelowNormal; } catch { }
                Baseline();
                using (Counters counters = new Counters())
                // Let event write failures propagate so EventCollector does not advance
                // its cursor past an event that never reached the log.
                using (EventCollector events = new EventCollector(Path.Combine(Program.DataRoot, "event-cursors.json"), record => WriteJsonLog("events", record)))
                {
                    Emit("activity", new Dictionary<string, object> { { "type", "counter_coverage" }, { "utc", Program.Utc() }, { "missing", counters.Missing } });
                    while (!stop.WaitOne(0))
                    {
                        Stopwatch cycle = Stopwatch.StartNew();
                        try { Sample(counters); } catch (Exception ex) { ErrorOnce("resources", ex); }
                        if (lastDetails == 0 || uptime.Elapsed.TotalSeconds - lastDetails >= Program.DetailInterval)
                        {
                            try { Processes(); } catch (Exception ex) { ErrorOnce("processes", ex); }
                            try { events.Poll(); } catch (Exception ex) { ErrorOnce("events", ex); }
                            try { logs.Cleanup(); } catch (Exception ex) { ErrorOnce("retention", ex); }
                            lastDetails = uptime.Elapsed.TotalSeconds;
                        }
                        SaveStatus(true);
                        if (Program.MaxSeconds > 0 && uptime.Elapsed.TotalSeconds >= Program.MaxSeconds) { stop.Set(); break; }
                        int remaining = Math.Max(1, (int)(Program.Interval * 1000 - cycle.ElapsedMilliseconds));
                        stop.WaitOne(remaining);
                    }
                }
            }
            catch (Exception ex) { ErrorOnce("worker", ex); }
            finally
            {
                Emit("activity", new Dictionary<string, object> { { "type", "stop" }, { "utc", Program.Utc() } });
                SaveStatus(false);
                finished = true;
            }
        }
        private void Sample(Counters counters)
        {
            double now = uptime.Elapsed.TotalSeconds;
            double elapsed = now - lastSampleSeconds;
            DateTime utcNow = DateTime.UtcNow;
            double wallElapsed = lastSampleUtc == default(DateTime) ? elapsed : (utcNow - lastSampleUtc).TotalSeconds;
            if (lastSampleSeconds > 0 && (elapsed > Program.Interval * 2.5 || wallElapsed > Program.Interval * 2.5)) Emit("activity", new Dictionary<string, object> { { "type", "sampling_gap" }, { "utc", Program.Utc() }, { "seconds", Math.Round(wallElapsed, 2) }, { "monotonic_seconds", Math.Round(elapsed, 2) }, { "interpretation", "sampling_interrupted_cause_unknown" } });
            Native.MemoryStatus memory = new Native.MemoryStatus { Length = (uint)Marshal.SizeOf(typeof(Native.MemoryStatus)) };
            if (!Native.GlobalMemoryStatusEx(ref memory)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            Native.PerformanceInfo performance = new Native.PerformanceInfo { Size = (uint)Marshal.SizeOf(typeof(Native.PerformanceInfo)) };
            bool info = Native.GetPerformanceInfo(ref performance, performance.Size);
            long idle, kernel, user;
            object cpu = null;
            if (Native.GetSystemTimes(out idle, out kernel, out user))
            {
                long total = kernel + user;
                long delta = total - lastTotal;
                if (lastTotal > 0 && delta > 0) cpu = Math.Round(Math.Max(0, Math.Min(100, 100.0 * (delta - (idle - lastIdle)) / delta)), 2);
                lastIdle = idle; lastTotal = total;
            }
            counters.Collect();
            double page = performance.PageSize.ToUInt64();
            using (Process self = Process.GetCurrentProcess())
            {
                double ownCpu = self.TotalProcessorTime.TotalSeconds;
                Dictionary<string, object> sample = new Dictionary<string, object>();
                sample["utc"] = Program.Utc();
                sample["elapsed_seconds"] = Math.Round(elapsed, 3);
                sample["cpu_pct"] = cpu;
                sample["ram_total_MiB"] = Math.Round(memory.TotalPhysical / 1048576.0, 2);
                sample["ram_used_MiB"] = Math.Round((memory.TotalPhysical - memory.AvailablePhysical) / 1048576.0, 2);
                sample["ram_available_MiB"] = Math.Round(memory.AvailablePhysical / 1048576.0, 2);
                sample["ram_used_pct"] = memory.MemoryLoad;
                sample["commit_MiB"] = info ? (object)Math.Round(performance.CommitTotal.ToUInt64() * page / 1048576.0, 2) : null;
                sample["commit_limit_MiB"] = info ? (object)Math.Round(performance.CommitLimit.ToUInt64() * page / 1048576.0, 2) : null;
                sample["commit_pct"] = info && performance.CommitLimit.ToUInt64() > 0 ? (object)Math.Round(100.0 * performance.CommitTotal.ToUInt64() / performance.CommitLimit.ToUInt64(), 2) : null;
                sample["kernel_nonpaged_MiB"] = info ? (object)Math.Round(performance.KernelNonpaged.ToUInt64() * page / 1048576.0, 2) : null;
                sample["kernel_paged_MiB"] = info ? (object)Math.Round(performance.KernelPaged.ToUInt64() * page / 1048576.0, 2) : null;
                sample["pages_input_per_sec"] = counters.Read("pages_input_per_sec", 1);
                sample["pages_output_per_sec"] = counters.Read("pages_output_per_sec", 1);
                sample["disk_queue"] = counters.Read("disk_queue", 1);
                sample["disk_read_ms"] = counters.Read("disk_read_ms", 1000);
                sample["disk_write_ms"] = counters.Read("disk_write_ms", 1000);
                sample["disk_read_Bps"] = counters.Read("disk_read_Bps", 1);
                sample["disk_write_Bps"] = counters.Read("disk_write_Bps", 1);
                sample["gpu_dedicated_MiB"] = counters.ReadArraySum("gpu_dedicated_MiB");
                sample["gpu_shared_MiB"] = counters.ReadArraySum("gpu_shared_MiB");
                sample["process_count"] = info ? (object)performance.ProcessCount : null;
                sample["monitor_working_MiB"] = Math.Round(self.WorkingSet64 / 1048576.0, 2);
                sample["monitor_private_MiB"] = Math.Round(self.PrivateMemorySize64 / 1048576.0, 2);
                sample["monitor_cpu_pct"] = lastSampleSeconds > 0 && elapsed > 0 ? (object)Math.Round(Math.Max(0, ownCpu - lastSelfCpu) / elapsed / Environment.ProcessorCount * 100, 4) : null;
                sample["monitor_pid"] = self.Id;
                bool saved = false;
                try { logs.Sample(sample); SetLogError("resources", null); saved = true; }
                catch (Exception ex) { SetLogError("resources", ex); ErrorOnce("resources_write", ex); }
                lock (statusGate) { latest = sample; if (saved) lastSuccessfulSampleUtc = Convert.ToString(sample["utc"], CultureInfo.InvariantCulture); }
                lastSelfCpu = ownCpu;
            }
            lastSampleSeconds = now;
            lastSampleUtc = utcNow;
        }
        private void Processes()
        {
            double now = uptime.Elapsed.TotalSeconds;
            HashSet<int> hung = new HashSet<int>();
            Native.EnumWindows((hwnd, param) => { if (Native.IsWindowVisible(hwnd) && Native.IsHungAppWindow(hwnd)) { uint id; Native.GetWindowThreadProcessId(hwnd, out id); hung.Add((int)id); } return true; }, IntPtr.Zero);
            List<Dictionary<string, object>> items = new List<Dictionary<string, object>>();
            Dictionary<int, Tuple<long, double>> next = new Dictionary<int, Tuple<long, double>>();
            int skipped = 0;
            foreach (Process process in Process.GetProcesses())
            {
                using (process)
                {
                    try
                    {
                        double seconds = process.TotalProcessorTime.TotalSeconds;
                        long started = process.StartTime.ToUniversalTime().Ticks;
                        object cpu = null;
                        Tuple<long, double> previous;
                        if (lastProcessSeconds > 0 && now > lastProcessSeconds && processCpu.TryGetValue(process.Id, out previous) && previous.Item1 == started) cpu = Math.Round(Math.Max(0, seconds - previous.Item2) / (now - lastProcessSeconds) / Environment.ProcessorCount * 100, 3);
                        next[process.Id] = Tuple.Create(started, seconds);
                        items.Add(new Dictionary<string, object> { { "name", process.ProcessName }, { "pid", process.Id }, { "working_MiB", Math.Round(process.WorkingSet64 / 1048576.0, 2) }, { "private_MiB", Math.Round(process.PrivateMemorySize64 / 1048576.0, 2) }, { "cpu_pct", cpu }, { "hung_window", hung.Contains(process.Id) } });
                    }
                    catch (System.ComponentModel.Win32Exception) { skipped++; }
                    catch (InvalidOperationException) { skipped++; }
                    catch (NotSupportedException) { skipped++; }
                }
            }
            IEnumerable<Dictionary<string, object>> selected = items.OrderByDescending(p => Convert.ToDouble(p["private_MiB"])).Take(8).Concat(items.Where(p => p["cpu_pct"] != null).OrderByDescending(p => Convert.ToDouble(p["cpu_pct"])).Take(5)).Concat(items.Where(p => (bool)p["hung_window"])).GroupBy(p => (int)p["pid"]).Select(g => g.First()).Take(30);
            Emit("processes", new Dictionary<string, object> { { "type", "process_snapshot" }, { "utc", Program.Utc() }, { "cpu_normalized_to_all_logical_processors", true }, { "selection", "top_private8_top_cpu5_hung_visible_windows" }, { "skipped_processes", skipped }, { "processes", selected.ToArray() } });
            processCpu.Clear();
            foreach (KeyValuePair<int, Tuple<long, double>> value in next) processCpu[value.Key] = value.Value;
            lastProcessSeconds = now;
        }
        protected override void ExitThreadCore() { stop.Set(); base.ExitThreadCore(); }
        protected override void Dispose(bool disposing)
        {
            if (!disposed && disposing)
            {
                disposed = true;
                stop.Set();
                worker.Join(10000);
                SystemEvents.SessionSwitch -= SessionChanged;
                exitTimer.Stop(); exitTimer.Dispose();
                if (tray != null) { tray.Visible = false; tray.ContextMenuStrip.Dispose(); tray.Dispose(); }
                window.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
