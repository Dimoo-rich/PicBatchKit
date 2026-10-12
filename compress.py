#!/usr/bin/env python3
"""Local image compression with a strict byte budget; originals are never changed."""
import argparse
import io
import json
import math
import zipfile
from pathlib import Path
from PIL import Image, ImageOps

EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

def encode(image, kind, quality=92):
    buf = io.BytesIO()
    options = {'optimize': True} if kind in {'JPEG', 'PNG'} else {'method': 6}
    if kind != 'PNG':
        options['quality'] = quality
    image.save(buf, format=kind, **options)
    return buf.getvalue()

def compress(source, target, edge, kind, ratio="original", fit="crop"):
    with Image.open(source) as raw:
        animated = getattr(raw, 'n_frames', 1) > 1
        raw.seek(0)
        image = ImageOps.exif_transpose(raw).convert('RGBA')
    original_dimensions = image.size
    transparent = image.getextrema()[3][0] < 255
    if ratio != 'original':
        a, b = (3, 4) if ratio == '3:4' else (1, 1)
        size = (max(1, round((edge or max(image.size)) * a / max(a,b))), max(1, round((edge or max(image.size)) * b / max(a,b))))
        if fit == 'crop':
            image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
        else:
            contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
            padded = Image.new('RGBA', size, 'white')
            padded.alpha_composite(contained, ((size[0]-contained.width)//2, (size[1]-contained.height)//2))
            image = padded
    if kind == 'JPEG':
        background = Image.new('RGB', image.size, 'white')
        background.paste(image, mask=image.getchannel('A'))
        image = background
    elif not transparent:
        image = image.convert('RGB')
    # Ensure output metadata contains no source EXIF, ICC, or text payloads.
    image.info.clear()
    if edge:
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    for _ in range(64):
        data = encode(image, kind)
        if len(data) < target:
            return data, image.size, original_dimensions, animated, transparent
        if kind != 'PNG':
            low = encode(image, kind, 10)
            if len(low) < target:
                best, lo, hi = low, 10, 92
                while lo <= hi:
                    q = (lo + hi) // 2
                    trial = encode(image, kind, q)
                    if len(trial) < target:
                        best, lo = trial, q + 1
                    else:
                        hi = q - 1
                return best, image.size, original_dimensions, animated, transparent
        if image.size == (1, 1):
            break
        image = image.resize(tuple(max(1, int(v * .8)) for v in image.size), Image.Resampling.LANCZOS)
    raise ValueError('无法达到目标文件大小，请提高目标 KB')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--target-kb', type=float, default=1000)
    parser.add_argument('--max-edge', type=int, default=1600)
    parser.add_argument('--format', choices=['jpg', 'webp', 'png'], default='jpg')
    parser.add_argument('--recursive', action='store_true')
    parser.add_argument('--ratio', choices=['original','3:4','1:1'], default='original')
    parser.add_argument('--fit', choices=['crop','pad'], default='crop')
    parser.add_argument('--numbered', action='store_true')
    parser.add_argument('--zip', action='store_true')
    args = parser.parse_args()
    if not math.isfinite(args.target_kb) or args.target_kb < 1 or args.max_edge < 0:
        parser.error('目标大小必须至少为 1 KB；最长边必须非负')
    sources, seen = [], set()
    for item in args.inputs:
        if not item.exists():
            parser.error(f'输入不存在：{item}')
        paths = sorted(item.rglob('*') if args.recursive else item.glob('*')) if item.is_dir() else [item]
        for path in paths:
            if path.is_file() and path.suffix.lower() in EXTS and path.resolve() not in seen:
                seen.add(path.resolve())
                sources.append(path.resolve())
    if not sources:
        parser.error('未找到 JPG、PNG、WebP 图片')
    out = args.output.resolve()
    for item in args.inputs:
        if item.is_dir() and (out == item.resolve() or args.recursive and item.resolve() in out.parents):
            parser.error('输出目录必须放在输入扫描目录之外')
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / 'compression-report.json'
    if report_path.exists():
        parser.error('输出目录已有报告，请选择一个新的输出目录')
    kind = {'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[args.format]
    target = int(args.target_kb * 1000)
    rows = []
    for index, source in enumerate(sources, 1):
        row = {'source': str(source), 'before_bytes': source.stat().st_size}
        try:
            data, dimensions, original, animated, transparent = compress(source, target, args.max_edge, kind, args.ratio, args.fit)
            stem = f'{index:02d}' if args.numbered else f'{source.stem}-compressed'
            destination = out / f'{stem}.{args.format}'
            number = 2
            while True:
                try:
                    with destination.open('xb') as file:
                        file.write(data)
                    break
                except FileExistsError:
                    destination = out / f'{stem}-{number}.{args.format}'
                    number += 1
            row.update(status='success', output=str(destination), after_bytes=len(data), target_bytes=target,
                       width=dimensions[0], height=dimensions[1], original_dimensions=original,
                       animated_first_frame=animated, transparency_to_white=transparent and kind == 'JPEG')
        except Exception as error:
            row.update(status='error', error=str(error))
        rows.append(row)
    report = {'success': sum(r['status'] == 'success' for r in rows),
              'failed': sum(r['status'] == 'error' for r in rows), 'files': rows}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.zip:
        zip_path = out / '配图.zip'
        with zipfile.ZipFile(zip_path, 'x', zipfile.ZIP_STORED) as archive:
            for row in rows:
                if row['status'] == 'success':
                    archive.write(row['output'], Path(row['output']).name)
            archive.write(report_path, report_path.name)
    print(json.dumps({'success': report['success'], 'failed': report['failed'], 'report': str(report_path)}, ensure_ascii=False))
    return 1 if report['failed'] else 0

if __name__ == '__main__':
    raise SystemExit(main())
