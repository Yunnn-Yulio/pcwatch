using System;
using System.Collections.Generic;
using System.Diagnostics.Eventing.Reader;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Xml;

namespace PCWatch
{
    // Reads a narrow subset of diagnostic events. It never writes to Windows logs.
    public sealed class EventCollector : IDisposable
    {
        public sealed class CursorState
        {
            public long Cursor { get; set; }
            public bool Initialized { get; set; }
            public string StartUtc { get; set; }
        }

        public sealed class SavedState
        {
            public int Version { get; set; }
            public Dictionary<string, CursorState> Logs { get; set; }
        }

        private const int MaximumEventsPerLog = 100;
        private readonly string stateFile;
        private readonly Action<IDictionary<string, object>> emit;
        private readonly JavaScriptSerializer serializer = new JavaScriptSerializer();
        private readonly Dictionary<string, CursorState> cursors = new Dictionary<string, CursorState>();
        private readonly Dictionary<string, string> failures = new Dictionary<string, string>();
        private readonly HashSet<string> unavailable = new HashSet<string>();
        private readonly HashSet<string> covered = new HashSet<string>();
        private bool disposed;
        private string stateFailure;

        private static readonly string[] LogNames = new string[] { "System", "Application" };
        private static readonly HashSet<string> NumericFields = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "BugcheckCode", "BugcheckParameter1", "BugcheckParameter2", "BugcheckParameter3", "BugcheckParameter4",
            "SleepInProgress", "PowerButtonTimestamp", "BootAppStatus", "Checkpoint", "CheckpointStatus",
            "ConnectedStandbyInProgress", "SystemSleepTransitionsToOn", "CsEntryScenarioInstanceId",
            "CsEntryScenarioInstanceIdV2", "BugcheckInfoFromEFI", "LongPowerButtonPressDetected",
            "LidReliability", "InputSuppressionState", "PowerButtonSuppressionState", "LidState", "WHEABootErrorCount",
            "ErrorSource", "ApicId", "MCABank", "MciStat", "MciAddr", "MciMisc", "ErrorType", "TransactionType",
            "Participation", "RequestType", "MemorIO", "MemHierarchyLvl", "Timeout", "OperationType", "Channel",
            "Length", "ValidBits", "ErrorStatus", "PhysicalAddress", "PhysicalAddressMask", "Node", "Card",
            "Module", "Bank", "Device", "Row", "Column", "BitPosition", "RankNumber", "CardHandle", "ModuleHandle",
            "Bus", "DeviceId", "Function", "Segment", "VendorID", "ClassCode",
            "Command", "Status", "BridgeControl", "BridgeStatus", "SleepState", "TargetState", "EffectiveState",
            "Reason", "Flags", "WakeSourceType", "WakeSourceLength", "TransitionsToOn", "ShutdownActionType",
            "ShutdownEventCode", "ShutdownReason", "Group", "Number", "CapDurationInSeconds", "PpcChanges",
            "TpcChanges", "PccChanges", "SystemCommitLimit", "SystemCommitCharge", "ProcessCommitCharge",
            "PagedPoolUsage", "NonPagedPoolUsage", "PhysicalMemorySize", "PhysicalMemoryUsage", "NumProcesses",
            "ExceptionCode", "FaultingOffset", "ProcessId", "ProcessCreationTime", "StartTime", "TerminationTime",
            "HangType", "BucketType", "CabId", "AppTimeStamp", "ModuleTimeStamp"
        };

        private static readonly HashSet<string> AppFields = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "AppName", "ApplicationName", "ModuleName", "FaultingApplicationName", "FaultingModuleName", "ExeFileName"
        };

        private static readonly HashSet<string> WerEventNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "APPCRASH", "APPHANG", "AppHangB1", "AppHangXProcB1", "AppHangTransient", "BEX", "BEX64",
            "BlueScreen", "LiveKernelEvent", "CLR20r3", "MoAppCrash", "MoAppHang", "MoBEX", "MoBEX64",
            "RADAR_PRE_LEAK_32", "RADAR_PRE_LEAK_64", "WindowsUpdateFailure3", "PnPDeviceProblemCode"
        };

        private static readonly Regex NumericValue = new Regex("^(?:0[xX][0-9a-fA-F]{1,32}|[0-9]{1,24}|[0-9a-fA-F]{1,16})$", RegexOptions.CultureInvariant);
        private static readonly Regex SafeAppName = new Regex("^[A-Za-z0-9 _+.()\\[\\]-]{1,120}\\.(?:exe|dll|sys|ocx|com|scr)$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        public EventCollector(string stateFile, Action<IDictionary<string, object>> emit)
        {
            if (String.IsNullOrWhiteSpace(stateFile)) throw new ArgumentException("A state file is required.", "stateFile");
            if (emit == null) throw new ArgumentNullException("emit");
            this.stateFile = stateFile;
            this.emit = emit;
            LoadState();
            foreach (string log in LogNames)
            {
                CursorState cursor;
                if (!cursors.TryGetValue(log, out cursor))
                {
                    cursors[log] = NewCursor();
                }
                else if (cursor == null || cursor.Cursor < 0)
                {
                    cursors[log] = NewCursor();
                }
                else if (!cursor.Initialized && !IsValidUtc(cursor.StartUtc))
                {
                    cursor.StartUtc = Utc(DateTime.UtcNow.AddHours(-2));
                }
            }
        }

        public void Poll()
        {
            if (disposed) throw new ObjectDisposedException("EventCollector");
            foreach (string log in LogNames)
            {
                if (unavailable.Contains(log)) continue;
                try
                {
                    PollLog(log);
                    failures.Remove(log);
                }
                catch (Exception ex)
                {
                    int code = ErrorCode(ex);
                    string key = ex.GetType().FullName + ":" + code.ToString(CultureInfo.InvariantCulture);
                    string prior;
                    bool permanent = ex is UnauthorizedAccessException || ex is EventLogNotFoundException ||
                                     code == 5 || code == 1314 || code == 15007;
                    if (!failures.TryGetValue(log, out prior) || !String.Equals(prior, key, StringComparison.Ordinal))
                    {
                        EmitCoverage(log, permanent ? "unavailable" : "error", ex);
                        failures[log] = key;
                    }
                    if (permanent) unavailable.Add(log);
                }
            }
            SaveState();
        }

        private void PollLog(string log)
        {
            long latest = ReadLatestRecordId(log);
            CursorState state = cursors[log];
            if (state.Initialized && latest < state.Cursor)
            {
                EmitCoverage(log, "reset", null);
                state = NewCursor();
                cursors[log] = state;
            }

            if (!covered.Contains(log) || failures.ContainsKey(log))
            {
                EmitCoverage(log, "available", null);
                covered.Add(log);
            }

            if (state.Initialized && latest == state.Cursor) return;
            if (latest == 0)
            {
                state.Cursor = 0;
                state.Initialized = true;
                return;
            }

            string range = state.Initialized
                ? "EventRecordID > " + state.Cursor.ToString(CultureInfo.InvariantCulture)
                : "TimeCreated[@SystemTime >= '" + state.StartUtc + "']";
            string query = BuildQuery(log, range, latest);
            EventLogQuery eventQuery = new EventLogQuery(log, PathType.LogName, query);
            eventQuery.ReverseDirection = false;
            int count = 0;
            using (EventLogReader reader = new EventLogReader(eventQuery))
            {
                while (count < MaximumEventsPerLog)
                {
                    using (EventRecord record = reader.ReadEvent())
                    {
                        if (record == null) break;
                        long id = record.RecordId.GetValueOrDefault();
                        emit(BuildEvent(log, record));
                        if (id > state.Cursor) state.Cursor = id;
                        state.Initialized = true;
                        count++;
                    }
                }
            }
            // Only advance to the snapshot end after draining all matching events.
            // At the cap, leave the cursor on the last emitted event for the next poll.
            if (count < MaximumEventsPerLog)
            {
                state.Cursor = latest;
                state.Initialized = true;
            }
        }

        private static long ReadLatestRecordId(string log)
        {
            EventLogQuery query = new EventLogQuery(log, PathType.LogName, "*");
            query.ReverseDirection = true;
            using (EventLogReader reader = new EventLogReader(query))
            using (EventRecord record = reader.ReadEvent())
            {
                return record == null ? 0 : record.RecordId.GetValueOrDefault();
            }
        }

        private static string BuildQuery(string log, string range, long latest)
        {
            // Several short selectors avoid the complexity limit of one XPath.
            StringBuilder xml = new StringBuilder();
            xml.Append("<QueryList><Query Id=\"0\">");
            foreach (string filter in FiltersFor(log))
            {
                string xpath = "*[System[(" + filter + ") and (" + range +
                               " and EventRecordID <= " + latest.ToString(CultureInfo.InvariantCulture) + ")]]";
                xml.Append("<Select Path=\"");
                xml.Append(log);
                xml.Append("\">");
                xml.Append(System.Security.SecurityElement.Escape(xpath));
                xml.Append("</Select>");
            }
            xml.Append("</Query></QueryList>");
            return xml.ToString();
        }

        private static string[] FiltersFor(string log)
        {
            if (log == "Application")
            {
                return new string[]
                {
                    "Provider[@Name='Application Hang'] and EventID=1002",
                    "Provider[@Name='Application Error'] and EventID=1000",
                    "Provider[@Name='Windows Error Reporting'] and EventID=1001"
                };
            }
            return new string[]
            {
                "Provider[@Name='Microsoft-Windows-WHEA-Logger']",
                "Provider[@Name='Microsoft-Windows-Resource-Exhaustion-Detector'] and EventID=2004",
                "Provider[@Name='Display'] and EventID=4101",
                "Provider[@Name='nvlddmkm']",
                "(Provider[@Name='disk'] or Provider[@Name='Disk']) and (EventID=7 or EventID=51 or EventID=153 or EventID=157)",
                "(Provider[@Name='stornvme'] or Provider[@Name='storahci']) and EventID=129",
                "Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41 or EventID=42 or EventID=107 or EventID=506 or EventID=507)",
                "Provider[@Name='Microsoft-Windows-Kernel-Processor-Power'] and EventID=37",
                "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1",
                "Provider[@Name='EventLog'] and (EventID=6005 or EventID=6006 or EventID=6008)"
            };
        }

        private static IDictionary<string, object> BuildEvent(string log, EventRecord record)
        {
            Dictionary<string, object> result = new Dictionary<string, object>();
            result["type"] = "event";
            result["log"] = log;
            result["provider"] = record.ProviderName;
            result["event_id"] = record.Id;
            result["level"] = record.Level.HasValue ? (object)record.Level.Value : null;
            result["utc"] = record.TimeCreated.HasValue ? Utc(record.TimeCreated.Value.ToUniversalTime()) : null;
            result["record_id"] = record.RecordId;
            Dictionary<string, object> data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            try
            {
                // XML is parsed in memory only. Neither XML nor rendered messages are logged.
                XmlReaderSettings settings = new XmlReaderSettings();
                settings.DtdProcessing = DtdProcessing.Prohibit;
                settings.XmlResolver = null;
                XmlDocument document = new XmlDocument();
                document.XmlResolver = null;
                using (StringReader text = new StringReader(record.ToXml()))
                using (XmlReader xml = XmlReader.Create(text, settings)) document.Load(xml);
                XmlNodeList namedData = document.SelectNodes("/*[local-name()='Event']/*[local-name()='EventData']/*[local-name()='Data']");
                foreach (XmlNode node in namedData)
                {
                    XmlAttribute name = node.Attributes == null ? null : node.Attributes["Name"];
                    if (name != null) AddAllowedField(data, name.Value, node.InnerText);
                }
                if (String.Equals(record.ProviderName, "Microsoft-Windows-WHEA-Logger", StringComparison.Ordinal))
                {
                    XmlNode raw = document.SelectSingleNode("/*[local-name()='Event']/*[local-name()='EventData']/*[local-name()='Data'][@Name='RawData']");
                    if (raw != null) AddCperSummary(data, raw.InnerText);
                }
                if (String.Equals(record.ProviderName, "Windows Error Reporting", StringComparison.Ordinal) && data.ContainsKey("EventName") &&
                    (String.Equals(Convert.ToString(data["EventName"]), "LiveKernelEvent", StringComparison.OrdinalIgnoreCase) || String.Equals(Convert.ToString(data["EventName"]), "BlueScreen", StringComparison.OrdinalIgnoreCase)))
                {
                    XmlNode code = document.SelectSingleNode("/*[local-name()='Event']/*[local-name()='EventData']/*[local-name()='Data'][@Name='P1']");
                    if (code != null && NumericValue.IsMatch(code.InnerText.Trim())) data["KernelEventCode"] = code.InnerText.Trim();
                }
                XmlNodeList userData = document.SelectNodes("/*[local-name()='Event']/*[local-name()='UserData']//*[not(*)]");
                foreach (XmlNode node in userData) AddAllowedField(data, node.LocalName, node.InnerText);
            }
            catch (Exception ex)
            {
                result["data_parse_error_type"] = ex.GetType().Name;
                result["data_parse_error_code"] = ErrorCode(ex);
            }
            result["data"] = data;
            return result;
        }

        private static void AddCperSummary(Dictionary<string, object> data, string raw)
        {
            // Only a public format header and section-type GUIDs survive; no payload,
            // record identifier, firmware serial number or raw dump is retained.
            if (raw == null || raw.Length < 256 || raw.Length > 2 * 1024 * 1024 || raw.Length % 2 != 0 || !raw.StartsWith("43504552", StringComparison.OrdinalIgnoreCase)) return;
            byte[] header = new byte[128];
            for (int i = 0; i < header.Length; i++) header[i] = Convert.ToByte(raw.Substring(i * 2, 2), 16);
            int count = BitConverter.ToUInt16(header, 10);
            uint flags = BitConverter.ToUInt32(header, 104);
            data["cper_severity"] = BitConverter.ToUInt32(header, 12);
            data["cper_flags"] = flags;
            data["cper_previous_error"] = (flags & 2) != 0;
            data["cper_sections_truncated"] = (flags & 64) != 0;
            data["cper_section_count"] = count;
            byte[] guidBytes = new byte[16];
            Array.Copy(header, 80, guidBytes, 0, 16);
            data["cper_notification_type"] = new Guid(guidBytes).ToString();
            List<string> types = new List<string>();
            for (int i = 0; i < Math.Min(count, 32); i++)
            {
                int offset = 128 + i * 72 + 16;
                if ((offset + 16) * 2 > raw.Length) break;
                for (int j = 0; j < 16; j++) guidBytes[j] = Convert.ToByte(raw.Substring((offset + j) * 2, 2), 16);
                types.Add(new Guid(guidBytes).ToString());
            }
            data["cper_section_types"] = types;
        }

        private static void AddAllowedField(Dictionary<string, object> data, string name, string value)
        {
            if (String.IsNullOrEmpty(name) || value == null) return;
            value = value.Trim();
            if (NumericFields.Contains(name))
            {
                bool boolean;
                if (Boolean.TryParse(value, out boolean)) data[name] = boolean;
                else if (NumericValue.IsMatch(value)) data[name] = value;
            }
            else if (AppFields.Contains(name))
            {
                // Any source path is discarded; only conservative executable basenames survive.
                string basename;
                try { basename = Path.GetFileName(value); }
                catch (ArgumentException) { return; }
                if (!String.IsNullOrEmpty(basename) && SafeAppName.IsMatch(basename)) data[name] = basename;
            }
            else if (String.Equals(name, "EventName", StringComparison.OrdinalIgnoreCase) && WerEventNames.Contains(value))
            {
                data[name] = value;
            }
        }

        private void EmitCoverage(string log, string status, Exception error)
        {
            Dictionary<string, object> result = new Dictionary<string, object>();
            result["type"] = "event_coverage";
            result["utc"] = Utc(DateTime.UtcNow);
            result["log"] = log;
            result["status"] = status;
            if (error != null)
            {
                result["error_type"] = error.GetType().Name;
                result["error_code"] = ErrorCode(error);
            }
            emit(result);
        }

        private void LoadState()
        {
            try
            {
                if (!File.Exists(stateFile)) return;
                FileInfo info = new FileInfo(stateFile);
                if (info.Length > 65536) throw new InvalidDataException();
                SavedState saved = serializer.Deserialize<SavedState>(File.ReadAllText(stateFile, Encoding.UTF8));
                if (saved == null || saved.Version != 1 || saved.Logs == null) throw new InvalidDataException();
                foreach (string log in LogNames)
                {
                    CursorState cursor;
                    if (saved.Logs.TryGetValue(log, out cursor)) cursors[log] = cursor;
                }
            }
            catch (Exception ex) { ReportStateError(ex); }
        }

        private void SaveState()
        {
            try
            {
                SavedState saved = new SavedState();
                saved.Version = 1;
                saved.Logs = cursors;
                string temporary = stateFile + ".tmp";
                File.WriteAllText(temporary, serializer.Serialize(saved), new UTF8Encoding(false));
                if (File.Exists(stateFile)) File.Replace(temporary, stateFile, null);
                else File.Move(temporary, stateFile);
                stateFailure = null;
            }
            catch (Exception ex) { ReportStateError(ex); }
        }

        private void ReportStateError(Exception ex)
        {
            string key = ex.GetType().FullName + ":" + ErrorCode(ex).ToString(CultureInfo.InvariantCulture);
            if (String.Equals(key, stateFailure, StringComparison.Ordinal)) return;
            stateFailure = key;
            Dictionary<string, object> result = new Dictionary<string, object>();
            result["type"] = "event_state_error";
            result["utc"] = Utc(DateTime.UtcNow);
            result["error_type"] = ex.GetType().Name;
            result["error_code"] = ErrorCode(ex);
            emit(result);
        }

        private static CursorState NewCursor()
        {
            return new CursorState { Cursor = 0, Initialized = false, StartUtc = Utc(DateTime.UtcNow.AddHours(-2)) };
        }

        private static bool IsValidUtc(string value)
        {
            DateTime parsed;
            return !String.IsNullOrEmpty(value) && DateTime.TryParseExact(value, "o", CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind, out parsed) && parsed.Kind == DateTimeKind.Utc;
        }

        private static string Utc(DateTime value)
        {
            return value.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture);
        }

        private static int ErrorCode(Exception ex)
        {
            System.ComponentModel.Win32Exception nativeError = ex as System.ComponentModel.Win32Exception;
            if (nativeError != null) return nativeError.NativeErrorCode;
            int hresult = ex.HResult;
            return (hresult & unchecked((int)0xffff0000)) == unchecked((int)0x80070000)
                ? hresult & 0xffff : hresult;
        }

        public void Dispose()
        {
            disposed = true;
        }
    }
}
