# svgfontembed

svgfontembed is a Python CLI tool that allows you to embed fonts in SVG files, ensuring that the text in your images is displayed correctly even without internet access or if the linked font is no longer available. This is also good for privacy, as font downloads can be used for tracking purposes.

## How it works

svgfontembed takes an SVG file as input and replaces any fonts that are fetched via a link with an embedded font, stripped down to only the subset of characters used within the text in the SVG. It does this by downloading the linked font and generating a base64 WOFF2 string, which is then used to replace the link in the SVG file. If an unused font is included and the `--keep-unused` option is not used, it is removed altogether.

The benefit of this is that the resulting SVG file will be self-contained and can be opened and displayed correctly on any device, even if the original font is no longer available. Additionally, the embedded font will only contain the characters that are actually used in the SVG, resulting in a smaller overall file size.

It's worth noting that the embedded font will not be cached by the browser, so if many SVG files use the same font, it may be more economical to just have the browser download the original font (though it will no longer be available offline).

**Reminder:** Please be sure to observe the license of any fonts you use with this tool.

## Installation

svgfontembed is available on PyPI. The recommended install method is to use `pipx`:

```bash
pipx install svgfontembed
```

It can can be installed with pip:

```bash
pip install svgfontembed
```

## Usage

To use svgfontembed, simply install it using pip and run the following command:

```bash
svgfontembed INPUT_SVG [OUTPUT_SVG] [--inplace] [--overwrite] [--keep-unused]
```


This will process the input SVG file and save the resulting file with embedded fonts to the specified output file. If `--inplace` is used, the input file will be overwritten with the output. If `--overwrite` is used, any existing files will be overwritten. If `--keep-unused` is used, fonts that are not used in the SVG will not be removed.

## Examples

Here are some examples of how you might use svgfontembed:

```bash
# Process input.svg and save to output.svg
svgfontembed input.svg output.svg

# Process input.svg and save to the current working directory
svgfontembed input.svg

# Process input.svg and overwrite it with the output
svgfontembed input.svg --inplace

# Process input.svg and save to output.svg, overwriting any existing files
svgfontembed input.svg output.svg --overwrite

# Process input.svg and save to output.svg, keeping unused fonts in the output
svgfontembed input.svg output.svg --keep-unused
```

## License

svgfontembed is licensed under the MIT license. See the LICENSE file for more details.

To-Do

- Clean up code and dependencies
- Test on more files (currently only files output from Excalidrawn have been tested)
- Load fonts which are already present as embedded base64 and strip unused characters
