// wtlaunch.cs - Win+X PowerShell entry wrapper
//
// Purpose: a tiny launcher that starts Windows Terminal (wt.exe) instead of the
// legacy PowerShell console host. It is meant to be compiled to a file named
// powershell.exe and referenced from the Start Menu shortcut that TWINUI uses
// for the Win+X "Windows PowerShell (I/A)" entries.
//
// Design notes:
//  - wt.exe is resolved at RUNTIME (no hardcoded user path), so the compiled
//    binary is portable across machines and user accounts.
//  - If wt.exe cannot be found, it falls back to the real powershell.exe, so a
//    broken/removed Windows Terminal never leaves Win+X dead.
//  - Assembly metadata mimics Windows PowerShell so shell UI that shows the
//    target's FileDescription still reads "Windows PowerShell".
//
// Build:  build-launcher.ps1   (uses the in-box .NET Framework csc.exe)

using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;

[assembly: AssemblyTitle("Windows PowerShell")]
[assembly: AssemblyDescription("Windows PowerShell")]
[assembly: AssemblyCompany("Microsoft Corporation")]
[assembly: AssemblyProduct("Microsoft(R) Windows(R) Operating System")]
[assembly: AssemblyCopyright("(C) Microsoft Corporation. All rights reserved.")]
[assembly: AssemblyFileVersion("10.0.19041.1")]
[assembly: AssemblyVersion("10.0.19041.1")]

internal static class WtLaunch
{
    private static string FallbackPowerShell()
    {
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe");
    }

    private static string ResolveWt()
    {
        // 1) explicit override (machine/user env var), useful for testing
        string env = Environment.GetEnvironmentVariable("WINXWT_TARGET");
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;

        // 2) the Store app-execution alias installed with Windows Terminal
        string alias = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            @"Microsoft\WindowsApps\wt.exe");
        if (File.Exists(alias)) return alias;

        // 3) anything named wt.exe on PATH
        string path = Environment.GetEnvironmentVariable("PATH");
        if (!string.IsNullOrEmpty(path))
        {
            foreach (string dir in path.Split(';'))
            {
                if (string.IsNullOrEmpty(dir)) continue;
                try
                {
                    string cand = Path.Combine(dir.Trim().Trim('"'), "wt.exe");
                    if (File.Exists(cand)) return cand;
                }
                catch { /* malformed PATH entry - ignore */ }
            }
        }
        return null;
    }

    private static int Main(string[] args)
    {
        string arguments = args.Length > 0 ? string.Join(" ", args) : string.Empty;
        string target = ResolveWt();

        if (target != null)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(target, arguments);
                psi.UseShellExecute = true;   // required for the alias reparse point
                Process.Start(psi);
                return 0;
            }
            catch { /* fall through to the legacy host */ }
        }

        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(FallbackPowerShell(), arguments);
            psi.UseShellExecute = true;
            Process.Start(psi);
            return 0;
        }
        catch
        {
            return 1;
        }
    }
}
