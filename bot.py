"""
MINE-CLOUD TCG BOT — VPS deployer WITHOUT LXC and WITHOUT /dev/kvm.

Runs full virtual machines using QEMU TCG (software emulation) through libvirt.
No container runtime, no hardware virtualization, no nested-KVM provider setting.
"""

import asyncio
import base64
import json
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import string
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import discord
import requests
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- configuration --------------------------------
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
PREFIX = os.getenv('BOT_PREFIX', '!').strip() or '!'
ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID', '0') or 0)
VPS_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '0') or 0)

IMAGE_DIR = Path(os.getenv('IMAGE_DIR', '/var/lib/libvirt/images/tcg-bot'))
NETWORK = os.getenv('LIBVIRT_NETWORK', 'default')
CPU_MODEL = os.getenv('VM_CPU_MODEL', 'qemu64')
VIRT_TYPE = 'qemu'  # TCG: never 'kvm'
DB_PATH = Path(os.getenv('TCG_BOT_DB', '/root/tcg-bot.db'))

DEFAULT_RAM = int(os.getenv('DEFAULT_RAM_GB', '1'))
DEFAULT_CPU = int(os.getenv('DEFAULT_CPU', '1'))
DEFAULT_DISK = int(os.getenv('DEFAULT_DISK_GB', '10'))
EXPIRY_DAYS = int(os.getenv('VPS_EXPIRY_DAYS', '30'))
PUBLIC_IP = os.getenv('PUBLIC_IP', '').strip()
SSH_PORT_MIN = int(os.getenv('SSH_PORT_MIN', '40000'))
SSH_PORT_MAX = int(os.getenv('SSH_PORT_MAX', '60000'))

IMAGES = {
    'ubuntu22': {
        'label': 'Ubuntu 22.04 LTS',
        'url': 'https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img',
    },
    'ubuntu24': {
        'label': 'Ubuntu 24.04 LTS',
        'url': 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img',
    },
    'debian12': {
        'label': 'Debian 12',
        'url': 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2',
    },
    'alpine320': {
        'label': 'Alpine 3.20',
        'url': 'https://dev.alpinelinux.org/~tomalok/alpine-cloud-images/v3.20/generic/x86_64/alpine-3.20.8-x86_64-bios-cloudinit-r0.qcow2',
    },
}

DB_LOCK = threading.Lock()
PROXY_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)


# ------------------------------- database -----------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        name TEXT UNIQUE NOT NULL,
        os TEXT NOT NULL,
        ram INTEGER NOT NULL,
        cpu INTEGER NOT NULL,
        disk INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        password TEXT NOT NULL,
        ssh_port INTEGER,
        ssh_pid INTEGER,
        created_at TEXT NOT NULL,
        expiry TEXT NOT NULL
    )''')
    return conn


def vps_by_name(name: str):
    with DB_LOCK:
        conn = db()
        try:
            row = conn.execute('SELECT * FROM vps WHERE name=?', (name,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def user_vps(uid: str):
    with DB_LOCK:
        conn = db()
        try:
            return [dict(r) for r in conn.execute('SELECT * FROM vps WHERE user_id=? ORDER BY id', (uid,))]
        finally:
            conn.close()


def all_vps():
    with DB_LOCK:
        conn = db()
        try:
            return [dict(r) for r in conn.execute('SELECT * FROM vps ORDER BY id')]
        finally:
            conn.close()


def insert_vps(data: dict):
    with DB_LOCK:
        conn = db()
        try:
            cur = conn.execute('''INSERT INTO vps
                (user_id,name,os,ram,cpu,disk,status,password,ssh_port,ssh_pid,created_at,expiry)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (
                data['user_id'], data['name'], data['os'], data['ram'], data['cpu'],
                data['disk'], 'running', data['password'], None, None,
                data['created_at'], data['expiry']))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_vps(name: str, **fields):
    if not fields:
        return
    sets = ','.join(f'{k}=?' for k in fields)
    with DB_LOCK:
        conn = db()
        try:
            conn.execute(f'UPDATE vps SET {sets} WHERE name=?', (*fields.values(), name))
            conn.commit()
        finally:
            conn.close()


def delete_vps(name: str):
    with DB_LOCK:
        conn = db()
        try:
            conn.execute('DELETE FROM vps WHERE name=?', (name,))
            conn.commit()
        finally:
            conn.close()


# ------------------------------- utilities ----------------------------------
async def run(*args, timeout=180, check=True):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f'Command timed out: {" ".join(args[:3])}')
    text = (out or b'').decode('utf-8', 'replace')
    if check and proc.returncode != 0:
        raise RuntimeError((err or b'').decode('utf-8', 'replace')[-1500:] or f'Command failed: {" ".join(args[:3])}')
    return text


def rand_password(length=20):
    alphabet = string.ascii_letters + string.digits + '!@#$%^&*'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def safe_name(username: str) -> str:
    base = re.sub(r'[^a-z0-9]', '', username.lower())[:12] or 'vps'
    return f'{base}-vm-{secrets.token_hex(2)}'


def public_ip() -> str:
    if PUBLIC_IP:
        return PUBLIC_IP
    try:
        return requests.get('https://api.ipify.org', timeout=5).text.strip()
    except Exception:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return 'YOUR_VPS_IP'


def embed(title, description, color=0x87CEFA):
    return discord.Embed(title=title, description=description, color=color)


def is_admin(uid: int) -> bool:
    return bool(ADMIN_ID and uid == ADMIN_ID)


# ------------------------------ libvirt helpers -----------------------------
async def ensure_network():
    await run('virsh', '-c', 'qemu:///system', 'net-autostart', NETWORK, check=False, timeout=30)
    await run('virsh', '-c', 'qemu:///system', 'net-start', NETWORK, check=False, timeout=30)


async def domain_state(name: str) -> str:
    try:
        out = await run('virsh', '-c', 'qemu:///system', 'domstate', name, timeout=20)
        return out.strip().lower()
    except Exception:
        return 'missing'


async def guest_ip(name: str) -> str:
    try:
        out = await run('virsh', '-c', 'qemu:///system', 'domifaddr', name, '--source', 'lease', timeout=25)
        m = re.search(r'\b(\d{1,3}(?:\.\d{1,3}){3})/\d+', out)
        return m.group(1) if m else ''
    except Exception:
        return ''


async def free_port() -> int:
    async with PROXY_LOCK:
        for _ in range(30):
            port = random.randint(SSH_PORT_MIN, SSH_PORT_MAX)
            s = socket.socket()
            try:
                s.bind(('0.0.0.0', port))
                return port
            except OSError:
                continue
            finally:
                s.close()
    raise RuntimeError('No free SSH proxy port found')


async def start_ssh_proxy(name: str, ip: str) -> tuple:
    if not ip:
        raise RuntimeError('Guest has no IP lease yet. Try `!ssh name` again in a minute.')
    port = await free_port()
    cmd = ['nohup', 'socat', f'TCP-LISTEN:{port},fork,reuseaddr', f'TCP:{ip}:22']
    log = IMAGE_DIR / f'{name}-socat.log'
    with open(log, 'ab') as fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=fh, stderr=fh, start_new_session=True)
    await asyncio.sleep(1)
    return port, proc.pid


async def stop_ssh_proxy(row: dict):
    pid = row.get('ssh_pid')
    if pid:
        try:
            os.kill(int(pid), 15)
        except (ProcessLookupError, ValueError, PermissionError):
            pass


USER_DATA = '''#cloud-config
ssh_pwauth: true
lock_passwd: false
disable_root: false
chpasswd:
  expire: false
  list: |
    root:__PASSWORD__
packages:
  - curl
  - ca-certificates
  - openssh-server
  - qemu-guest-agent
runcmd:
  - sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true
  - sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
  - systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true
  - systemctl enable --now qemu-guest-agent 2>/dev/null || true
'''


def network_config(mac: str) -> str:
    return f'''version: 2
ethernets:
  eth0:
    match:
      macaddress: {mac}
    dhcp4: true
    optional: true
'''


async def create_vm(name: str, os_key: str, ram: int, cpu: int, disk_gb: int, password: str) -> dict:
    if os_key not in IMAGES:
        raise RuntimeError('Unsupported OS')
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    await ensure_network()

    info = IMAGES[os_key]
    base = IMAGE_DIR / os.path.basename(info['url'].split('?')[0])
    disk = IMAGE_DIR / f'{name}.qcow2'
    ud = IMAGE_DIR / f'{name}-user-data.yaml'
    nc = IMAGE_DIR / f'{name}-network-config.yaml'

    if not base.exists():
        tmp = str(base) + '.part'
        await run('curl', '-fL', '--retry', '3', '-o', tmp, info['url'], timeout=900)
        os.replace(tmp, base)

    await run('qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base), str(disk), timeout=180)
    await run('qemu-img', 'resize', str(disk), f'{disk_gb}G', timeout=120)

    ud.write_text(USER_DATA.replace('__PASSWORD__', password), encoding='utf-8')

    mac_bytes = bytearray(secrets.token_bytes(6))
    mac_bytes[0] = (mac_bytes[0] & 0xFE) | 0x02
    mac = ':'.join(f'{b:02x}' for b in mac_bytes)
    nc.write_text(network_config(mac), encoding='utf-8')

    args = [
        'virt-install', '--connect', 'qemu:///system',
        '--virt-type', VIRT_TYPE, '--name', name,
        '--memory', str(ram * 1024), '--vcpus', str(cpu),
        '--disk', f'path={disk},format=qcow2,bus=virtio',
        '--network', f'network={NETWORK},model=virtio,mac={mac}',
        '--cpu', CPU_MODEL, '--import', '--graphics', 'none',
        '--noautoconsole', '--console', 'pty,target_type=serial',
        '--cloud-init', f'user-data={ud},network-config={nc}',
    ]
    try:
        await run(*args, timeout=420)
    finally:
        for f in (ud, nc):
            try:
                f.unlink()
            except OSError:
                pass

    for _ in range(30):
        state = await domain_state(name)
        if state not in ('missing', ''):
            break
        await asyncio.sleep(2)
    return {'name': name, 'state': state, 'disk': str(disk)}


# ------------------------------- UI classes ---------------------------------
class OSView(discord.ui.View):
    def __init__(self, target_user, ram, cpu, disk, admin=False):
        super().__init__(timeout=180)
        self.target_user = target_user
        self.ram, self.cpu, self.disk = ram, cpu, disk
        self.admin = admin
        options = [discord.SelectOption(label=v['label'], value=k) for k, v in IMAGES.items()]
        sel = discord.ui.Select(placeholder='Choose operating system', options=options)
        sel.callback = self.on_select
        self.add_item(sel)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != (self.target_user if self.admin else interaction.user.id):
            await interaction.response.send_message('Ye selection aapki nahi hai.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        os_key = interaction.data['values'][0]
        name = safe_name(interaction.user.name)
        password = rand_password()
        try:
            await create_vm(name, os_key, self.ram, self.cpu, self.disk, password)
            insert_vps({
                'user_id': str(interaction.user.id), 'name': name, 'os': os_key,
                'ram': self.ram, 'cpu': self.cpu, 'disk': self.disk,
                'password': password,
                'created_at': datetime.now().isoformat(),
                'expiry': (datetime.now() + timedelta(days=EXPIRY_DAYS)).isoformat(),
            })
        except Exception as exc:
            await interaction.followup.send(embed=embed('Deploy failed', str(exc)[:1800], 0xFF5555), ephemeral=True)
            return
        await interaction.followup.send(embed=embed(
            'VM Created',
            f'**Name:** `{name}`\n**OS:** {IMAGES[os_key]["label"]}\n'
            f'**Specs:** {self.ram}GB / {self.cpu} vCPU / {self.disk}GB\n'
            f'**Root password:** `{password}`\n\n'
            f'Public SSH ke liye: `!ssh {name}`', 0x55FF99), ephemeral=True)


class ManageView(discord.ui.View):
    def __init__(self, row):
        super().__init__(timeout=300)
        self.row = row

    async def check(self, interaction):
        if str(interaction.user.id) != self.row['user_id'] and not is_admin(interaction.user.id):
            await interaction.response.send_message('Ye VPS aapki nahi hai.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Start', style=discord.ButtonStyle.green)
    async def start(self, interaction, button):
        if not await self.check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await run('virsh', '-c', 'qemu:///system', 'start', self.row['name'], timeout=60)
            update_vps(self.row['name'], status='running')
            await interaction.followup.send('VM started.', ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc)[:900], ephemeral=True)

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.red)
    async def stop(self, interaction, button):
        if not await self.check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await run('virsh', '-c', 'qemu:///system', 'shutdown', self.row['name'], check=False, timeout=30)
            update_vps(self.row['name'], status='stopped')
            await interaction.followup.send('Shutdown requested.', ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc)[:900], ephemeral=True)

    @discord.ui.button(label='Restart', style=discord.ButtonStyle.blurple)
    async def restart(self, interaction, button):
        if not await self.check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await run('virsh', '-c', 'qemu:///system', 'reboot', self.row['name'], timeout=60)
            update_vps(self.row['name'], status='running')
            await interaction.followup.send('Reboot requested.', ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc)[:900], ephemeral=True)

    @discord.ui.button(label='SSH', style=discord.ButtonStyle.gray)
    async def ssh(self, interaction, button):
        if not await self.check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if await domain_state(self.row['name']) != 'running':
                await run('virsh', '-c', 'qemu:///system', 'start', self.row['name'], timeout=60)
            await asyncio.sleep(4)
            ip = await guest_ip(self.row['name'])
            port, pid = await start_ssh_proxy(self.row['name'], ip)
            update_vps(self.row['name'], ssh_port=port, ssh_pid=pid, status='running')
            await interaction.followup.send(embed=embed(
                'SSH Ready',
                f'`ssh root@{public_ip()} -p {port}`\n'
                f'Password: `{self.row["password"]}`\n\n'
                f'Guest IP: `{ip}` (libvirt NAT)', 0x55FF99), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc)[:1200], ephemeral=True)

    @discord.ui.button(label='Delete', style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        if not await self.check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await stop_ssh_proxy(self.row)
            await run('virsh', '-c', 'qemu:///system', 'destroy', self.row['name'], check=False, timeout=30)
            await run('virsh', '-c', 'qemu:///system', 'undefine', self.row['name'], '--remove-all-storage', timeout=60)
            delete_vps(self.row['name'])
            await interaction.followup.send('VM deleted.', ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc)[:1200], ephemeral=True)


# -------------------------------- commands ----------------------------------
@bot.event
async def on_ready():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    db().close()
    print(f'Logged in as {bot.user} | mode=QEMU-TCG | virt-type={VIRT_TYPE}')


@bot.command()
async def ping(ctx):
    await ctx.send(embed=embed('Pong', 'MINE-CLOUD TCG bot zinda hai. Na LXC, na /dev/kvm.'))


@bot.command()
async def mode(ctx):
    await ctx.send(embed=embed(
        'Virtualization Mode',
        f'**Backend:** QEMU TCG (software emulation)\n'
        f'**virt-type:** `{VIRT_TYPE}`\n'
        f'**LXC:** disabled\n'
        f'**/dev/kvm:** not required\n'
        f'**CPU model:** `{CPU_MODEL}`\n\n'
        f'VMs chalenge, par performance slow hogi.', 0xFFCC55))


@bot.command()
async def deploy(ctx):
    row = [discord.SelectOption(label=v['label'], value=k) for k, v in IMAGES.items()]
    view = OSView(ctx.author.id, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK)
    await ctx.send(embed=embed(
        'Deploy VM',
        f'Default plan: **{DEFAULT_RAM}GB RAM / {DEFAULT_CPU} vCPU / {DEFAULT_DISK}GB disk**\n'
        f'OS chuno (QEMU TCG, bina /dev/kvm ke):', 0x87CEFA), view=view)


@bot.command()
@commands.check(lambda ctx: is_admin(ctx.author.id))
async def create(ctx, ram: int, cpu: int, disk: int, user: discord.Member):
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send('Specs positive hone chahiye.')
        return
    view = OSView(user.id, ram, cpu, disk, admin=True)
    await ctx.send(embed=embed('Admin Deploy', f'User: {user.mention} | {ram}GB/{cpu}vCPU/{disk}GB', 0x87CEFA), view=view)


@bot.command(name='mylvps')
async def my_vps(ctx):
    rows = user_vps(str(ctx.author.id))
    if not rows:
        await ctx.send(embed=embed('My VPS', 'Aapke paas abhi koi VM nahi hai. `!deploy` se banayein.'))
        return
    lines = []
    for r in rows:
        lines.append(f"`{r['name']}` — {IMAGES.get(r['os'], {}).get('label', r['os'])} — {r['ram']}GB/{r['cpu']}vCPU — {r['status']}")
    await ctx.send(embed=embed('My VPS', '\n'.join(lines)))


@bot.command()
@commands.check(lambda ctx: is_admin(ctx.author.id))
async def allvps(ctx):
    rows = all_vps()
    if not rows:
        await ctx.send('Koi VM nahi hai.')
        return
    lines = [f"`{r['name']}` — <@{r['user_id']}> — {r['status']}" for r in rows]
    await ctx.send(embed=embed('All VPS', '\n'.join(lines[:40])))


@bot.command()
async def manage(ctx, name: str):
    row = vps_by_name(name)
    if not row:
        await ctx.send(embed=embed('Not found', 'Ye VM nahi mili.', 0xFF5555))
        return
    if str(ctx.author.id) != row['user_id'] and not is_admin(ctx.author.id):
        await ctx.send('Ye VPS aapki nahi hai.')
        return
    state = await domain_state(name)
    await ctx.send(embed=embed(
        f'Manage `{name}`',
        f'**State:** {state}\n**Specs:** {row["ram"]}GB / {row["cpu"]} vCPU / {row["disk"]}GB\n'
        f'**Expires:** {row["expiry"][:10]}', 0x87CEFA), view=ManageView(row))


@bot.command()
async def ssh(ctx, name: str):
    row = vps_by_name(name)
    if not row:
        await ctx.send('VM nahi mili.')
        return
    if str(ctx.author.id) != row['user_id'] and not is_admin(ctx.author.id):
        await ctx.send('Ye VPS aapki nahi hai.')
        return
    try:
        if await domain_state(name) != 'running':
            await run('virsh', '-c', 'qemu:///system', 'start', name, timeout=60)
        await asyncio.sleep(4)
        ip = await guest_ip(name)
        port, pid = await start_ssh_proxy(name, ip)
        update_vps(name, ssh_port=port, ssh_pid=pid, status='running')
        await ctx.send(embed=embed(
            'SSH Ready',
            f'`ssh root@{public_ip()} -p {port}`\nPassword: `{row["password"]}`', 0x55FF99))
    except Exception as exc:
        await ctx.send(embed=embed('SSH Error', str(exc)[:1500], 0xFF5555))


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'your_discord_bot_token_here':
        raise SystemExit('DISCORD_TOKEN missing in .env')
    if not shutil.which('virsh') or not shutil.which('virt-install') or not shutil.which('qemu-img') or not shutil.which('socat'):
        raise SystemExit('Run install.sh first (virsh, virt-install, qemu-img, socat required).')
    bot.run(TOKEN)
