from __future__ import annotations

import asyncio
import base64
import re
import sys
import tempfile
from dataclasses import dataclass
from functools import cached_property, lru_cache
from itertools import chain
from pathlib import Path
from typing import NamedTuple, Iterable

import httpx
import tinycss
import typer
from fontTools.subset import load_font, Options, save_font, Subsetter
from loguru import logger
from parsel import Selector
from tinycss.css21 import Declaration, AtRule, RuleSet

httpx_client = httpx.AsyncClient()


# About using regexes to parse XML: don't try this at home, kids.

# Regex to parse the font-faces
font_face_regex = re.compile(r"@font-face\s*{[^}]*}", re.MULTILINE)
# Regex to extract the src url from the font-face
src_regex = re.compile(r"src:\s*url\(([^)]*)\)")
# Regex to extract the font-family
font_family_regex = re.compile(r"font-family:\s*([^;]*)")


def get_text_from_svg(svg_contents: str, family: str | None = None) -> list[str]:
    """Extracts all the text contents in an SVG file.

    Args:
        svg_contents: A string containing the contents of an SVG file.
        family: The font family to filter the text by. If not provided, all text in the SVG file will be returned.

    Returns:
        A list of strings representing the text contents of the SVG file.
    """
    sel = Selector(svg_contents)
    if family:
        # Get text elements with a font-family attribute
        font_text = sel.xpath(f".//text[contains(@font-family, '{family}')]/text()").getall()

        # Get text elements matching a selector that uses the font-family
        for css in sel.css("style::text"):
            # todo - parse from ((?:local\(([^)]+)\)|url\(([^)]+)\))(?:[^;]+;\s*))*
            stylesheet = tinycss.make_parser().parse_stylesheet(css.get())
            declarations: Iterable[tuple[RuleSet, Declaration]]
            declarations = (
                (rule, declaration)
                for rule in stylesheet.rules
                for declaration in rule.declarations
                if declaration.name == "font-family"
                and any(family in rule_font for rule_font in declaration.value.as_css().split(","))
            )
            for rule, declaration in declarations:
                value = declaration.value.as_css()
                logger.debug(f"Found font-family usage: {value}")
                selector_css = rule.selector.as_css()
                rule_text = sel.css(selector_css).css("::text").getall()
                font_text.extend(rule_text)
                # todo (maybe) - Currently, we get all characters of all variations
                #  of the font. For instance, if the same font family uses bold and
                #  regular, we get all the characters for both. An improvement
                #  would be to only get the characters for the font family that
                #  matches the font-family attribute of the text element.
                #  To do this, we'd need to parse the font-family attribute of the
                #  text element and match it to the font-family in the CSS rule.

        return font_text
    #
    else:
        return Selector(svg_contents).xpath(".//text/text()").getall()


@dataclass
class FontFace:
    """A class to represent a font-face.


    Example font_face_definition inputs (from the SVG):

      @font-face {
        font-family: "Font";
        src: url("https://somesite.com/Font.woff2");
      }

      @font-face {
        font-family: "Fira Code";
        src: local("FiraCode-Regular"),
                url("https://cdnjs.cloudflare.com/ajax/libs/firacode/6.2.0/woff2/FiraCode-Regular.woff2") format("woff2"),
                url("https://cdnjs.cloudflare.com/ajax/libs/firacode/6.2.0/woff/FiraCode-Regular.woff") format("woff");
        font-style: normal;
        font-weight: 400;
      }
    """

    font_face_definition: str
    font_file_name: str | None = None

    @cached_property
    def src_url(self) -> str | None:
        if src := src_regex.search(self.font_face_definition):
            return src.group(1).strip("\"' ")
        return None

    @cached_property
    def font_family(self) -> str | None:
        if font_name := font_family_regex.search(self.font_face_definition):
            return font_name.group(1).strip("\"' ")
        return None

    @classmethod
    def from_svg(cls, svg_contents: str) -> tuple[FontFace, ...]:
        return tuple(FontFace(definition) for definition in font_face_regex.findall(svg_contents))

    @lru_cache(maxsize=1)
    async def get_font_contents(self) -> bytes | None:
        """Asynchronously downloads the font file from the source URL specified in the font-face
        definition.

        Returns:
            The contents of the font file.
        """

        if not self.src_url:
            logger.warning(f"Font face {self.font_family} has no src url.")
            return None

        self.font_file_name = Path(self.src_url).name

        logger.info(f"Downloading self {self.font_family} from {self.src_url}")
        font_req = await httpx_client.get(self.src_url)
        font_req.raise_for_status()
        font_contents = await font_req.aread()
        logger.info(f"Font downloaded: {self.font_family} ({len(font_contents) / 1024:.2f}kb)")
        return font_contents

    def __hash__(self) -> int:
        return hash(self.font_face_definition)


class SubsetDefinitionResult(NamedTuple):
    svg_contents: str
    total_fonts_size: int


async def get_font_subset_definition(
    font_face: FontFace, characters: set[str]
) -> SubsetDefinitionResult | None:
    """Subsets a font file for a given set of characters and generates the font-face definition for
    the subsetted font.

    Args:
        font_face: A `FontFace` object.
        characters: A set of characters to include in the font subset.

    Returns:
        A `SubsetDefinitionResult` object containing the subsetted SVG contents and the total
            size of the subsetted fonts. If the font file cannot be downloaded or subsetted,
            `None` is returned.
    """

    font_contents = await font_face.get_font_contents()

    if not font_contents:
        logger.warning(f"Unable to get font for {font_face.font_family}, skipping")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        name_from_url = font_face.src_url.split("/")[-1] if font_face.src_url else None
        font_file_name = name_from_url or font_face.font_file_name or "font.woff2"
        font_file = tmpdir_path / font_file_name
        font_size = font_file.write_bytes(font_contents)
        options = Options(flavor="woff2")
        font = load_font(font_file, options)
        subsetter = Subsetter(options)
        subsetter.populate(text="".join(characters))
        subsetter.subset(font)

        subset_file = tmpdir_path / "subset.woff2"
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
        return SubsetDefinitionResult(result, font_size)


def replace_escaped_unicode(svg_contents: str) -> str:
    """Replace escaped unicode characters with their actual unicode characters.

    Example:
        &#x2019; -> ’ (unicode character)

    Args:
        svg_contents: The SVG contents to replace the escaped unicode characters in.

    Returns:
        The SVG contents with the escaped unicode characters replaced.
    """
    svg_contents = re.sub(r"&#(\d+);", lambda g: chr(int(g.group(1))), svg_contents)
    return svg_contents


class EmbedFontsResult(NamedTuple):
    svg_contents: str
    total_fonts_size: int


async def embed_fonts(svg_contents: str, keep_unused_fonts: bool) -> EmbedFontsResult:
    """Embeds fonts in an SVG file.

    This function handles the process of downloading and subseting fonts, and replacing the
    font-face definitions in the SVG file with the subsetted font-face definitions.

    Args:
        svg_contents: The SVG file contents.
        keep_unused_fonts: Whether to keep fonts that are not used in the SVG file.

    Returns:
        An `EmbedFontsResult` object containing the SVG contents with embedded fonts and the
            total size of the subsetted fonts.
    """
    total_fonts_size = 0
    font_faces = FontFace.from_svg(svg_contents)
    families = [
        font.font_family if font.font_family is not None else "(unknown name)"
        for font in font_faces
    ]
    if not families:
        logger.warning("No fonts found in SVG")
        # raise typer.Exit(1)
    else:
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
                    if face.src_url:
                        req = await httpx_client.head(face.src_url)
                        total_fonts_size += int(req.headers.get("content-length", 0))
                        logger.success(
                            f"Saved {total_fonts_size / 1024:.2f}kb by removing unused font"
                        )
                except (httpx.HTTPError, ValueError, TypeError):
                    logger.warning(f"Unable to get size of {face.src_url}")
            else:
                logger.warning(r"Keeping unused font face {face.font_family}.")

    logger.info(f"Processing {len(to_process)} font faces.")
    for face, characters in to_process:
        subset_result = await get_font_subset_definition(face, characters)
        if subset_result is None:
            continue
        subset_definition, original_font_size = subset_result
        svg_contents = svg_contents.replace(face.font_face_definition, subset_definition)
        total_fonts_size += original_font_size
    return EmbedFontsResult(svg_contents, total_fonts_size)


async def main(
    svg_contents: str,
    keep_unused_fonts: bool,
    do_replace_escaped_unicode: bool,
    do_embed_fonts: bool,
) -> str:
    """Main function.

    Args:
        svg_contents: The SVG file contents.
        keep_unused_fonts: Whether to keep fonts that are not used in the SVG file.
        do_replace_escaped_unicode: Whether to replace escaped unicode characters with their
            actual unicode characters.
        do_embed_fonts: Whether to embed fonts in the SVG file.

    Returns:
        The SVG file contents with embedded fonts.
    """

    original_size = len(svg_contents)
    total_fonts_size = 0

    if do_replace_escaped_unicode:
        svg_contents = replace_escaped_unicode(svg_contents)

    if do_embed_fonts:
        result = await embed_fonts(svg_contents, keep_unused_fonts)
        svg_contents = result.svg_contents
        total_fonts_size = result.total_fonts_size

    logger.info(f"Original SVG size: {original_size / 1024:.2f}kb")
    logger.info(f"New SVG size: {len(svg_contents) / 1024:.2f}kb")
    logger.info(f"Size of fonts that would've been downloaded: {total_fonts_size / 1024:.2f}kb")
    saved = original_size + total_fonts_size - len(svg_contents)
    if saved > 0:
        logger.success(f"Saved {saved / 1024:.2f}kb in total")
    else:
        logger.warning(f"Didn't save any space (result is {saved / 1024:.2f}kb larger)")
    return svg_contents


app = typer.Typer(name="svgembedfont", no_args_is_help=True, add_completion=False)


@app.command(no_args_is_help=True)
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
        "-i",
        help="Overwrite the input file with the output.",
    ),
    overwrite_existing: bool = typer.Option(
        False,
        "--overwrite-existing",
        "-f",
        help="Overwrite existing files.",
    ),
    do_embed_fonts: bool = typer.Option(
        True,
        "--embed-fonts",
        "-e",
        help="Embed fonts in the SVG.",
    ),
    keep_unused_fonts: bool = typer.Option(
        False,
        "--keep-unused",
        "-k",
        help="Keep fonts which are not used in the SVG.",
    ),
    do_replace_escaped_unicode: bool = typer.Option(
        False,
        "--replace-escaped-unicode",
        "-u",
        help="Replace escaped unicode characters (e.g. `&#10245;` with their Unicode  )",
    ),
) -> None:
    """Embed fonts in an SVG file, using only the subset of characters actually present.

    This is useful for reducing the size of SVG files, makes them available offline, and
    prevents tracking by font providers.

    The tool can optionally also replace escaped unicode characters with their actual
    unicode characters, which can reduce the size of the SVG file, in some cases significantly.
    """

    if inplace and output != Path("."):
        logger.error("Cannot use --inplace and --output together.")
        raise typer.Exit(1)

    if output != Path("-"):
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

    svg_contents = asyncio.run(
        main(
            svg_contents=svg_contents,
            keep_unused_fonts=keep_unused_fonts,
            do_replace_escaped_unicode=do_replace_escaped_unicode,
            do_embed_fonts=do_embed_fonts,
        )
    )

    if output == Path("-"):
        sys.stdout.write(svg_contents)
    else:
        output.write_text(svg_contents)

    logger.success(f"Done! Wrote to {str(output) if output != Path('-') else 'stdout'}")


app()
