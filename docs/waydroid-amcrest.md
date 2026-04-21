# Running Amcrest Smart Home on Waydroid (Android Emulator)

This documents the process of installing Waydroid (an Android container) on tank2 (Ubuntu/Debian),
getting the Amcrest Smart Home app installed, and the unresolved networking issue to continue later.

## Prerequisites

- Linux server with KVM support (`egrep -c '(vmx|svm)' /proc/cpuinfo` should return > 0)
- GPU present (`ls /dev/dri`)
- Remote desktop access (xrdp or similar)

## 1. Install Waydroid

Waydroid is not in the standard apt repos. Add the official PPA first:

```bash
curl https://repo.waydro.id | sudo bash
sudo apt install waydroid
```

## 2. Initialize Waydroid

```bash
sudo waydroid init
```

This downloads the Android system and vendor images. Takes a few minutes.

## 3. Install a Wayland compositor (needed for xrdp sessions)

xrdp uses X11, not Wayland. Waydroid requires Wayland. Install Weston to bridge the gap:

```bash
sudo apt install weston
```

## 4. Start Waydroid

Each time you want to use Waydroid, do the following from within your xrdp/remote desktop session:

```bash
# Start the container service
sudo systemctl start waydroid-container

# Launch a Wayland compositor inside the X11 session
DISPLAY=:10.0 weston --backend=x11-backend.so &

# Inside the Weston window that appears, open a terminal and run:
waydroid show-full-ui
```

The Android UI will appear inside the Weston window.

## 5. Install ARM translation (required for ARM64 apps on x86_64)

tank2 is x86_64 but most Android apps ship ARM64 binaries. Install the translation layer:

```bash
sudo apt install lzip
cd /tmp
git clone https://github.com/casualsnek/waydroid_script
cd waydroid_script
sudo pip3 install -r requirements.txt --break-system-packages
sudo python3 main.py install libndk
```

## 6. Fix networking

After each reboot, iptables rules are lost. Re-apply them:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -s 192.168.240.0/24 -j MASQUERADE
sudo iptables-legacy -t nat -A POSTROUTING -s 192.168.240.0/24 -j MASQUERADE
```

### Known issue: no default gateway in Waydroid

As of this writing, Waydroid's dnsmasq does not advertise a default gateway in its DHCP offers,
so the Android container gets an IP (192.168.240.112) but no route to the internet.

The dnsmasq command waydroid runs is missing `--dhcp-option=3,192.168.240.1`. A workaround is
to kill and restart dnsmasq manually with the gateway option added:

```bash
sudo kill $(cat /run/waydroid-lxc/dnsmasq.pid)
sudo dnsmasq --conf-file=/dev/null -u dnsmasq --strict-order --bind-interfaces \
  --pid-file=/run/waydroid-lxc/dnsmasq.pid \
  --listen-address 192.168.240.1 \
  --dhcp-range 192.168.240.2,192.168.240.254 \
  --dhcp-lease-max=253 --dhcp-no-override \
  --except-interface=lo --interface=waydroid0 \
  --dhcp-leasefile=/var/lib/misc/dnsmasq.waydroid0.leases \
  --dhcp-authoritative \
  --dhcp-option=3,192.168.240.1 \
  --dhcp-option=6,8.8.8.8,8.8.4.4
```

Then force Android to renew its DHCP lease:

```bash
sudo waydroid shell dhcpcd eth0
```

This may or may not be sufficient — networking was working on a previous session but stopped
working after a reboot. **This is the outstanding issue to resolve before the Amcrest app can log in.**

## 7. Install Aurora Store (Play Store alternative)

Aurora Store allows installing Play Store apps without a Google account.

```bash
curl -L -o aurora.apk "https://f-droid.org/repo/com.aurora.store_64.apk"
adb install aurora.apk
```

Or install via `waydroid app install aurora.apk` if adb is not available.

## 8. Download Amcrest Smart Home APK

The app was not installable via Aurora Store due to device compatibility checks. Instead,
download the XAPK from APKPure inside the Waydroid browser, then install via command line.

The downloaded XAPK will be in `/sdcard/Download/` inside Waydroid, which maps to
`/home/chris/.local/share/waydroid/data/media/0/Download/` on the host (verify with
`sudo waydroid shell ls /sdcard/Download/`).

Extract and install:

```bash
# Copy xapk to host tmp and extract
sudo cp "/home/chris/.local/share/waydroid/data/media/0/Download/Amcrest Smart Home_*.xapk" /tmp/amcrest.zip
cd /tmp && sudo unzip amcrest.zip -d amcrest

# Copy APKs into the container's writable /data partition
sudo cp /tmp/amcrest/*.apk /home/chris/.local/share/waydroid/data/local/tmp/

# Install (include arm64 and xxxhdpi split APKs)
sudo waydroid shell pm install \
  /data/local/tmp/com.mm.android.amcrestsmarthome.apk \
  /data/local/tmp/config.arm64_v8a.apk \
  /data/local/tmp/config.en.apk \
  /data/local/tmp/config.xxxhdpi.apk
```

The app installs successfully. However, logging in fails with "Network error" until the
networking issue in step 6 is resolved.

## Outstanding work

- [ ] Fix Waydroid networking so Android has internet access after reboot
- [ ] Log into Amcrest Smart Home app and verify camera access
- [ ] Determine if the camera stream can be extracted for use in smart-home
