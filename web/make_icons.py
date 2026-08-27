from pathlib import Path
import struct, zlib

ROOT = Path(__file__).resolve().parent
BG = (9, 12, 17)
PANEL = (20, 27, 36)
GREEN = (96, 227, 156)
WHITE = (245, 247, 250)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def save_png(path: Path, size: int, pixels: bytearray) -> None:
    stride = size * 3
    raw = b''.join(b'\x00' + bytes(pixels[y*stride:(y+1)*stride]) for y in range(size))
    data = b'\x89PNG\r\n\x1a\n'
    data += png_chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
    data += png_chunk(b'IDAT', zlib.compress(raw, 9))
    data += png_chunk(b'IEND', b'')
    path.write_bytes(data)


def make(size: int, path: Path, maskable: bool=False) -> None:
    px = bytearray(BG * (size * size))

    def setp(x, y, c):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 3
            px[i:i+3] = bytes(c)

    def rect(x0, y0, x1, y1, c):
        x0=max(0,int(x0));y0=max(0,int(y0));x1=min(size,int(x1));y1=min(size,int(y1))
        row = bytes(c) * max(0, x1-x0)
        for y in range(y0,y1):
            i=(y*size+x0)*3;px[i:i+len(row)] = row

    def circle(cx, cy, r, c):
        cx=int(cx);cy=int(cy);r=int(r);rr=r*r
        for y in range(cy-r, cy+r+1):
            dy=(y-cy)*(y-cy)
            for x in range(cx-r,cx+r+1):
                if (x-cx)*(x-cx)+dy <= rr:setp(x,y,c)

    # Keep the installed PWA icon visually identical to the web icon.
    # The central F mark already sits safely inside Android's maskable safe zone.
    pad = int(size * 0.09)
    rect(pad,pad,size-pad,size-pad,PANEL)
    # Geometric F mark, deliberately simple and readable at small sizes.
    x=int(size*.33); y=int(size*.29); w=int(size*.13); h=int(size*.44)
    rect(x,y,x+w,y+h,WHITE)
    rect(x,y,int(size*.70),y+int(size*.11),WHITE)
    rect(x,y+int(size*.17),int(size*.62),y+int(size*.27),WHITE)
    circle(size*.70,size*.25,size*.05,GREEN)
    save_png(path,size,px)

make(192, ROOT/'icon-192.png')
make(512, ROOT/'icon-512.png')
make(512, ROOT/'icon-maskable-512.png', True)
