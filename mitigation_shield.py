import subprocess
import argparse
import sys
import time

parser = argparse.ArgumentParser(description='Active Defense: Intel CAT/MBA Controller')
parser.add_argument('--attacker_core', type=str, required=True, help='The CPU core running the Noisy Neighbor')
parser.add_argument('--action', choices=['baseline', 'isolate', 'strangulate'], required=True, help='The mitigation policy to apply')
args = parser.parse_args()

def run_pqos(command):
    """Executes a pqos command and handles errors."""
    try:
        # -I flag uses the OS resctrl interface you enabled in GRUB
        full_cmd = ["sudo", "pqos", "-I", "-e"] + command
        subprocess.run(full_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        print(f"[-] Hardware execution failed. Is resctrl mounted?\nError: {e.stderr.decode()}")
        sys.exit(1)

print(f"[*] Initializing Active Defense on Core {args.attacker_core}...")

if args.action == 'baseline':
    print("[*] Policy: BASELINE (Open all gates)")
    # 0x7FF gives full L3 cache access. MBA 100 gives full bandwidth.
    run_pqos([f"llc:{args.attacker_core}=0x7FF"])
    run_pqos([f"mba:{args.attacker_core}=100"])
    print("[+] Core resources fully restored.")

elif args.action == 'isolate':
    print("[*] Policy: ISOLATE (L3 Cache Partitioning)")
    # Trap the attacker in the lowest 2 ways of the cache (0x003)
    # Give the victim (Core 0) the upper ways (e.g., 0x7FC) - assuming Core 0 is victim
    run_pqos([f"llc:{args.attacker_core}=0x003"])
    run_pqos([f"llc:0=0x7FC"]) 
    print("[+] Attacker cache masked to 0x003. Victim cache protected.")

elif args.action == 'strangulate':
    print("[*] Policy: STRANGULATE (Maximal Throttling)")
    # Trap the cache AND choke the memory bandwidth down to 10%
    run_pqos([f"llc:{args.attacker_core}=0x003"])
    run_pqos([f"mba:{args.attacker_core}=10"])
    print("[+] Attacker cache masked and memory bandwidth throttled to 10%.")

# Verify the changes applied successfully
print("\n[*] Current Hardware Allocation State:")
subprocess.run(["sudo", "pqos", "-I", "-s"])
