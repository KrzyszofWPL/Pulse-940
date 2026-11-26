param(
    [switch]$Install,
    [switch]$Headless
)

$QemuBin = "C:\Users\HARDPC\qemu\qemu-system-aarch64.exe"
$CodeFd  = "C:\Users\HARDPC\qemu\share\edk2-aarch64-code.fd"
$VmDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$VarsFd  = Join-Path $VmDir "flash-vars.fd"
$DiskImg = Join-Path $VmDir "pulse940.qcow2"
$IsoImg  = Join-Path $VmDir "alpine-virt-3.24.4-aarch64.iso"

$ArgsList = @(
    "-M", "virt",
    "-cpu", "cortex-a53",
    "-smp", "4",
    "-m", "1024",
    "-drive", "if=pflash,format=raw,readonly=on,file=$CodeFd",
    "-drive", "if=pflash,format=raw,file=$VarsFd",
    "-drive", "if=virtio,file=$DiskImg",
    "-netdev", "user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::9000-:9000",
    "-device", "virtio-net-pci,netdev=net0"
)

if ($Install) {
    $ArgsList += @("-cdrom", $IsoImg)
}

if ($Headless) {
    $ArgsList += @("-display", "none", "-serial", "tcp:127.0.0.1:4444,server,nowait")
} else {
    $ArgsList += @("-nographic")
}

& $QemuBin @ArgsList
