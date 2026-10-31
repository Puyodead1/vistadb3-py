// Test oracle: drives the proprietary VistaDB 3.5 DLL through its public API only.
// Used to generate sample databases and to check the Python library against the engine.
//
//   oracle sql   <db|->  <file.sql>      run statements separated by lines containing only GO
//   oracle dump  <db>    <out.json> [sql] dump schema + rows (DDA, or "sql" = SELECT * per table)
//   oracle pack  <db>                    PackDatabase in place
//   oracle repair <db>                   RepairDatabase in place
using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using System.Text;
using VistaDB;
using VistaDB.DDA;
using VistaDB.Provider;

class JsonOut
{
    readonly TextWriter t;
    public JsonOut(TextWriter t) { this.t = t; }
    public JsonOut Append(string s) { t.Write(s); return this; }
    public JsonOut Append(char c) { t.Write(c); return this; }
    public JsonOut Append(int i) { t.Write(i.ToString(CultureInfo.InvariantCulture)); return this; }
    public JsonOut Append(object o) { t.Write(Convert.ToString(o, CultureInfo.InvariantCulture)); return this; }
}

static class Oracle
{
    static int Main(string[] args)
    {
        Thread_Culture();
        try
        {
            switch (args[0])
            {
                case "sql": return Sql(args[1], args[2]);
                case "dump": return Dump(args[1], args[2], args.Length > 3 && args[3] == "sql");
                case "pack": return Pack(args[1]);
                case "repair": return Repair(args[1]);
            }
            Console.Error.WriteLine("unknown command");
            return 2;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERROR: " + e);
            return 1;
        }
    }

    static void Thread_Culture()
    {
        System.Threading.Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture;
    }

    static int Sql(string db, string file)
    {
        string text = File.ReadAllText(file, Encoding.UTF8);
        var batches = new List<string>();
        var sb = new StringBuilder();
        foreach (string line in text.Replace("\r\n", "\n").Split('\n'))
        {
            if (line.Trim() == "GO") { batches.Add(sb.ToString()); sb.Length = 0; }
            else sb.Append(line).Append('\n');
        }
        if (sb.ToString().Trim().Length > 0) batches.Add(sb.ToString());
        using (var conn = db == "-" ? new VistaDBConnection() : new VistaDBConnection("Data Source=" + db))
        {
            if (db != "-") conn.Open();
            foreach (string b in batches)
            {
                if (b.Trim().Length == 0) continue;
                using (var cmd = new VistaDBCommand(b, conn)) cmd.ExecuteNonQuery();
            }
        }
        return 0;
    }

    static int Pack(string db)
    {
        using (IVistaDBDDA dda = VistaDBEngine.Connections.OpenDDA())
            dda.PackDatabase(db, null, false, null);
        return 0;
    }

    static int Repair(string db)
    {
        using (IVistaDBDDA dda = VistaDBEngine.Connections.OpenDDA())
            dda.RepairDatabase(db, null, null);
        return 0;
    }

    static int Dump(string db, string outFile, bool viaSql)
    {
        using (var sw = new StreamWriter(outFile, false, new UTF8Encoding(false))) {
        var w = new JsonOut(sw);
        using (IVistaDBDDA dda = VistaDBEngine.Connections.OpenDDA())
        using (IVistaDBDatabase d = dda.OpenDatabase(db, VistaDBDatabaseOpenMode.NonexclusiveReadOnly, null))
        {
            w.Append("{\"page_size\":").Append(d.PageSize)
             .Append(",\"lcid\":").Append(d.Culture.LCID)
             .Append(",\"case_sensitive\":").Append(d.CaseSensitive ? "true" : "false")
             .Append(",\"description\":"); Str(w, d.Description);
            w.Append(",\"tables\":[");
            var names = new List<string>();
            foreach (object n in d.EnumTables()) names.Add((string)n);
            names.Sort(StringComparer.Ordinal);
            bool firstT = true;
            foreach (string name in names)
            {
                if (!firstT) w.Append(','); firstT = false;
                IVistaDBTableSchema s = d.TableSchema(name);
                w.Append("\n{\"name\":"); Str(w, name);
                w.Append(",\"description\":"); Str(w, s.Description);
                w.Append(",\"columns\":[");
                for (int i = 0; i < s.ColumnCount; i++)
                {
                    IVistaDBColumnAttributes c = s[i];
                    if (i > 0) w.Append(',');
                    w.Append("{\"name\":"); Str(w, c.Name);
                    w.Append(",\"type\":\"").Append(c.Type).Append('"');
                    w.Append(",\"max_length\":").Append(c.MaxLength);
                    w.Append(",\"allow_null\":").Append(c.AllowNull ? "true" : "false");
                    w.Append(",\"read_only\":").Append(c.ReadOnly ? "true" : "false");
                    w.Append(",\"packed\":").Append(c.Packed ? "true" : "false");
                    w.Append(",\"encrypted\":").Append(c.Encrypted ? "true" : "false");
                    w.Append(",\"code_page\":").Append(c.CodePage);
                    w.Append(",\"description\":"); Str(w, c.Description);
                    w.Append(",\"caption\":"); Str(w, c.Caption);
                    w.Append('}');
                }
                w.Append("],\"indexes\":[");
                var idx = new List<IVistaDBIndexInformation>();
                foreach (object e in Items(s.Indexes)) idx.Add((IVistaDBIndexInformation)e);
                idx.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name));
                for (int i = 0; i < idx.Count; i++)
                {
                    var ix = idx[i];
                    if (i > 0) w.Append(',');
                    w.Append("{\"name\":"); Str(w, ix.Name);
                    w.Append(",\"expression\":"); Str(w, ix.KeyExpression);
                    w.Append(",\"primary\":").Append(ix.Primary ? "true" : "false");
                    w.Append(",\"unique\":").Append(ix.Unique ? "true" : "false");
                    w.Append(",\"fts\":").Append(ix.FTS ? "true" : "false");
                    w.Append(",\"fk\":").Append(ix.FKConstraint ? "true" : "false");
                    w.Append('}');
                }
                w.Append("],\"identities\":[");
                bool f = true;
                foreach (object e in Items(s.Identities))
                {
                    var id = (IVistaDBIdentityInformation)e;
                    if (!f) w.Append(','); f = false;
                    w.Append("{\"column\":"); Str(w, id.ColumnName);
                    w.Append(",\"seed\":"); Str(w, id.SeedValue);
                    w.Append(",\"step\":"); Str(w, id.StepExpression);
                    w.Append('}');
                }
                w.Append("],\"defaults\":[");
                f = true;
                foreach (object e in Items(s.Defaults))
                {
                    var dv = (IVistaDBDefaultValueInformation)e;
                    if (!f) w.Append(','); f = false;
                    w.Append("{\"column\":"); Str(w, dv.ColumnName);
                    w.Append(",\"expression\":"); Str(w, dv.Expression);
                    w.Append(",\"use_in_update\":").Append(dv.UseInUpdate ? "true" : "false");
                    w.Append('}');
                }
                w.Append("],\"rows\":[");
                if (viaSql) SqlRows(w, db, name);
                else DdaRows(w, d, name, s.ColumnCount);
                w.Append("]}");
            }
            w.Append("]}\n");
        }
        }
        return 0;
    }

    static IEnumerable<object> Items(object list)
    {
        foreach (object o in (IEnumerable)list)
        {
            if (o is DictionaryEntry) yield return ((DictionaryEntry)o).Value;
            else
            {
                var p = o.GetType().GetProperty("Value");
                yield return p != null && !(o is IVistaDBIndexInformation) ? p.GetValue(o, null) : o;
            }
        }
    }

    static void DdaRows(JsonOut w, IVistaDBDatabase d, string name, int cols)
    {
        using (IVistaDBTable t = d.OpenTable(name, false, true))
        {
            t.First();
            bool first = true;
            while (!t.EndOfTable)
            {
                if (!first) w.Append(','); first = false;
                w.Append("\n[");
                for (int i = 0; i < cols; i++)
                {
                    if (i > 0) w.Append(',');
                    IVistaDBValue v = t.Get(i);
                    Val(w, v.IsNull ? null : v.Value);
                }
                w.Append(']');
                t.Next();
            }
        }
    }

    static void SqlRows(JsonOut w, string db, string name)
    {
        using (var conn = new VistaDBConnection("Open Mode=NonExclusiveReadWrite;Data Source=" + db))
        {
            var adapter = new VistaDBDataAdapter("SET OPTIMIZATION ON; SELECT * FROM [" + name + "]", conn);
            var dt = new DataTable();
            adapter.Fill(dt);
            bool first = true;
            foreach (DataRow r in dt.Rows)
            {
                if (!first) w.Append(','); first = false;
                w.Append("\n[");
                for (int i = 0; i < dt.Columns.Count; i++)
                {
                    if (i > 0) w.Append(',');
                    Val(w, r[i] == DBNull.Value ? null : r[i]);
                }
                w.Append(']');
            }
        }
    }

    static void Val(JsonOut w, object o)
    {
        if (o == null) { w.Append("null"); return; }
        if (o is string) { Str(w, (string)o); return; }
        if (o is bool) { w.Append((bool)o ? "true" : "false"); return; }
        if (o is byte[]) { w.Append("{\"$bin\":\"").Append(Convert.ToBase64String((byte[])o)).Append("\"}"); return; }
        if (o is DateTime) { w.Append("{\"$dt\":\"").Append(((DateTime)o).ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff", CultureInfo.InvariantCulture)).Append("\"}"); return; }
        if (o is decimal) { w.Append("{\"$dec\":\"").Append(((decimal)o).ToString(CultureInfo.InvariantCulture)).Append("\"}"); return; }
        if (o is double) { w.Append("{\"$f\":\"").Append(((double)o).ToString("R", CultureInfo.InvariantCulture)).Append("\"}"); return; }
        if (o is float) { w.Append("{\"$f\":\"").Append(((float)o).ToString("R", CultureInfo.InvariantCulture)).Append("\"}"); return; }
        if (o is Guid) { w.Append("{\"$guid\":\"").Append(((Guid)o).ToString()).Append("\"}"); return; }
        if (o is byte || o is short || o is int || o is long) { w.Append(Convert.ToString(o, CultureInfo.InvariantCulture)); return; }
        Str(w, "?" + o.GetType().FullName + ":" + o);
    }

    static void Str(JsonOut w, string s)
    {
        if (s == null) { w.Append("null"); return; }
        w.Append('"');
        foreach (char ch in s)
        {
            switch (ch)
            {
                case '"': w.Append("\\\""); break;
                case '\\': w.Append("\\\\"); break;
                case '\n': w.Append("\\n"); break;
                case '\r': w.Append("\\r"); break;
                case '\t': w.Append("\\t"); break;
                default:
                    if (ch < 0x20 || (ch >= 0xD800 && ch <= 0xDFFF)) w.Append("\\u").Append(((int)ch).ToString("x4"));
                    else w.Append(ch);
                    break;
            }
        }
        w.Append('"');
    }
}
