from __future__ import annotations

import asyncio
import base64
import re
import sys
import tempfile
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path

import httpx
import typer
from fontTools.subset import load_font, Options, save_font, Subsetter
from loguru import logger
from parsel import Selector

httpx_client = httpx.AsyncClient()


# About using regexes to parse XML: don't try this at home, kids.

# Regex to parse the font-faces
font_face_regex = re.compile(r"@font-face\s*{[^}]*}", re.MULTILINE)
# Regex to extract the src url from the font-face
src_regex = re.compile(r"src:\s*url\(([^)]*)\)")
# Regex to extract the font-family
font_family_regex = re.compile(r"font-family:\s*([^;]*)")


def get_text_from_svg(svg_contents: str, family: str | None = None) -> list[str]:
    if family:
        selector = f".//text[contains(@font-family, '{family}')]/text()"
        return Selector(svg_contents).xpath(selector).getall()
    else:
        return Selector(svg_contents).xpath(".//text/text()").getall()


@dataclass
class FontFace:
    """A class to represent a font-face.


    Example font_face_definition input (from the SVG):

      @font-face {
        font-family: "Virgil";
        src: url("https://somesite.com/Font.woff2");
      }
    """

    font_face_definition: str
    font_file_name: str | None = None

    @cached_property
    def src_url(self) -> str | None:
        if src := src_regex.search(self.font_face_definition):
            return src.group(1).strip("\"' ")

    @cached_property
    def font_family(self) -> str | None:
        if font_name := font_family_regex.search(self.font_face_definition):
            return font_name.group(1).strip("\"' ")
        return None

    @classmethod
    def from_svg(cls, svg_contents: str) -> tuple[FontFace]:
        return tuple(FontFace(definition) for definition in font_face_regex.findall(svg_contents))

    @lru_cache(maxsize=1)
    async def get_font_contents(self) -> bytes | None:
        # todo - cache on disk

        if not self.src_url:
            logger.warning(f"Font face {self.font_family} has no src url.")
            return

        self.font_file_name = Path(self.src_url).name

        logger.info(f"Downloading self {self.font_family} from {self.src_url}")
        font_req = await httpx_client.get(self.src_url)
        font_req.raise_for_status()
        font_contents = await font_req.aread()
        logger.info(f"Font downloaded: {self.font_family} ({len(font_contents) / 1024:.2f}kb)")
        return font_contents

    def __hash__(self):
        return hash(self.font_face_definition)


async def get_font_subset_definition(
    font_face: FontFace, characters: set[str]
) -> tuple[str, int] | None:
    font_contents = await font_face.get_font_contents()

    if not font_contents:
        logger.warning(f"Unable to get font for {font_face.font_family}, skipping")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        font_file = tmpdir / font_face.src_url.split("/")[-1]
        font_size = font_file.write_bytes(font_contents)
        options = Options(flavor="woff2")
        font = load_font(font_file, options)
        subsetter = Subsetter(options)
        subsetter.populate(text="".join(characters))
        subsetter.subset(font)

        subset_file = tmpdir / "subset.woff2"
        save_font(font, subset_file, options=subsetter.options)
        message = (
            f"Success! {font_face.font_family} subsetted to {len(characters)} characters."
            f"File size: {subset_file.stat().st_size / 1024:.1f}kb "
            f"(was {font_file.stat().st_size / 1024:.2f}kb)"
        )
        logger.success(message)
        bs = subset_file.read_bytes()
        encoded = base64.b64encode(bs).decode("utf-8")
        src_line = f"src: url('data:font/woff2;base64,{encoded}') format('woff2');"
        result = re.sub(r"src:\s*url\(([^)]*)\)\s*;", src_line, font_face.font_face_definition)
        return result, font_size


async def main(
    svg_contents: str,
    keep_unused_fonts: bool,
) -> str:
    original_size = len(svg_contents)
    total_fonts_size = 0

    font_faces = FontFace.from_svg(svg_contents)
    families = [
        font.font_family if font.font_family is not None else "(unknown name)"
        for font in font_faces
    ]
    if not families:
        logger.warning("No fonts found in SVG")
        raise typer.Exit(1)
    logger.info(f"Found {len(font_faces)} font faces: {', '.join(families)}")

    to_process = []
    for face in font_faces:
        text = get_text_from_svg(svg_contents, face.font_family)
        characters = set("".join(text))
        if characters:
            to_process.append((face, characters))
            logger.info(f"Font face {face.font_family} uses {len(characters)} unique characters.")
        else:
            if not keep_unused_fonts:
                logger.warning(f"Font face {face.font_family} has no used characters.")
                logger.warning(r"I'll just throw this away ¯\_(ツ)_/¯)")
                logger.warning(r"Set the --keep-unused-fonts flag to keep it.")
                svg_contents = svg_contents.replace(face.font_face_definition, "")
                try:
                    req = await httpx_client.head(face.src_url)
                    total_fonts_size += int(req.headers.get("content-length", 0))
                    logger.success(f"Saved {total_fonts_size / 1024:.2f}kb by removing unused font")
                except (httpx.HTTPError, ValueError):
                    logger.warning(f"Unable to get size of {face.src_url}")
            else:
                logger.warning(r"Keeping unused font face {face.font_family}.")

    logger.info(f"Processing {len(to_process)} font faces.")
    for face, characters in to_process:
        subset_definition, original_font_size = await get_font_subset_definition(face, characters)

        if subset_definition is not None:
            svg_contents = svg_contents.replace(face.font_face_definition, subset_definition)
            total_fonts_size += original_font_size

    logger.info(f"Original SVG size: {original_size / 1024:.2f}kb")
    logger.info(f"New SVG size: {len(svg_contents) / 1024:.2f}kb")
    logger.info(f"Size of fonts that would've been downloaded: {total_fonts_size / 1024:.2f}kb")
    saved = original_size + total_fonts_size - len(svg_contents)
    if saved > 0:
        logger.success(f"Saved {saved / 1024:.2f}kb in total")
    else:
        logger.warning(f"Didn't save any space (result is {saved / 1024:.2f}kb larger)")
    return svg_contents


app = typer.Typer()


@app.command()
def svg_font_embed(
    input_svg: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        allow_dash=True,
        help="The SVG file to process.",
        metavar="INPUT_SVG",
        show_default=False,
    ),
    output: Path = typer.Argument(
        Path("."),
        exists=False,
        dir_okay=True,
        allow_dash=True,
        writable=True,
        help="Output file or directory.",
        metavar="OUTPUT_SVG",
    ),
    inplace: bool = typer.Option(
        False,
        "--inplace",
        help="Overwrite the input file with the output.",
    ),
    overwrite_existing: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing files.",
    ),
    keep_unused_fonts: bool = typer.Option(
        False,
        "--keep-unused",
        help="Keep fonts which are not used in the SVG.",
    ),
) -> None:
    """Embed fonts in an SVG file, using only the subset of characters actually present."""

    if inplace and output != Path("."):
        logger.error("Cannot use --inplace and --output together.")
        raise typer.Exit(1)

    if output == Path("-"):
        logger.info("Writing to stdout")
        output = sys.stdout
    else:
        if output.is_dir():
            output = (output / input_svg.name).with_stem(input_svg.stem + "_subset")

        if inplace:
            output = input_svg
        elif not output:
            output = Path.cwd().with_stem(input_svg.stem + "_subset")

        if output.exists() and not overwrite_existing and not inplace:
            logger.error(f"Output file {output} already exists.")
            raise typer.Exit(1)

        logger.info(f"Writing to {output}")

    if input_svg == Path("-"):
        logger.info("Reading from stdin")
        svg_contents = sys.stdin.read()
    else:
        logger.info(f"Reading from {input_svg}")
        svg_contents = input_svg.read_text()

    svg_contents = asyncio.run(main(svg_contents, keep_unused_fonts))

    if output == sys.stdout:
        output.write(svg_contents)
    else:
        output.write_text(svg_contents)

    logger.success("Done!")


app()
