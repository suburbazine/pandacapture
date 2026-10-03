# Linux USB permissions

Without these rules, only root can open the panda, and PandaCapture reports "permission denied".

```bash
sudo cp linux/50-pandacapture.rules /etc/udev/rules.d/
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug the panda and plug it back in. The rules cover the panda running firmware, its
bootstub (flasher), and the STM32 bootloader used the first time you flash.
