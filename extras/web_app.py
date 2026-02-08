from flask import Flask, request, render_template_string, send_file, abort
import os
import sys
import ipaddress
from urllib.parse import urlparse

# Import functions from onefilellm.py.
# Ensure onefilellm.py is accessible in the same directory.
from onefilellm import process_github_repo, process_github_pull_request, process_github_issue
from onefilellm import process_arxiv_pdf, process_local_folder, fetch_youtube_transcript
from onefilellm import crawl_and_extract_text, process_doi_or_pmid, get_token_count, preprocess_text, safe_file_read
from pathlib import Path
import pyperclip
from rich.console import Console

app = Flask(__name__)
console = Console()

# Directory where output files are written; downloads are restricted to this directory.
OUTPUT_DIR = os.path.abspath(".")

# Allowed filenames that may be downloaded via /download.
ALLOWED_DOWNLOAD_FILES = {"uncompressed_output.txt", "compressed_output.txt"}

# Simple HTML template using inline rendering for demonstration.
template = """
<!DOCTYPE html>
<html>
<head>
    <title>1FileLLM Web Interface</title>
    <style>
    body { font-family: sans-serif; margin: 2em; }
    input[type="text"] { width: 80%; padding: 0.5em; }
    .output-container { margin-top: 2em; }
    .file-links { margin-top: 1em; }
    pre { background: #f8f8f8; padding: 1em; border: 1px solid #ccc; }
    </style>
</head>
<body>
    <h1>1FileLLM Web Interface</h1>
    <form method="POST" action="/">
        <p>Enter a URL, DOI, or PMID:</p>
        <input type="text" name="input_path" required placeholder="e.g. https://github.com/jimmc414/1filellm or 10.1234/example"/>
        <button type="submit">Process</button>
    </form>

    {% if output %}
    <div class="output-container">
        <h2>Processed Output</h2>
        <pre>{{ output }}</pre>

        <h3>Token Counts</h3>
        <p>Uncompressed Tokens: {{ uncompressed_token_count }}<br>
        Compressed Tokens: {{ compressed_token_count }}</p>

        <div class="file-links">
            <a href="/download?filename=uncompressed_output.txt">Download Uncompressed Output</a> |
            <a href="/download?filename=compressed_output.txt">Download Compressed Output</a>
        </div>
    </div>
    {% endif %}
</body>
</html>
"""


def _is_safe_url(url: str) -> bool:
    """Reject URLs targeting private/internal networks (SSRF protection)."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # Reject common metadata endpoints and localhost
        if hostname in ("localhost", "metadata.google.internal"):
            return False
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except ValueError:
            pass  # hostname is a domain name, not an IP
        return True
    except Exception:
        return False


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        input_path = request.form.get("input_path", "").strip()

        # Prepare filenames
        output_file = "uncompressed_output.txt"
        processed_file = "compressed_output.txt"
        urls_list_file = "processed_urls.txt"

        # Determine input type and process accordingly (mirroring logic from onefilellm.py main)
        try:
            parsed = urlparse(input_path)

            if "github.com" in input_path:
                if not _is_safe_url(input_path):
                    return render_template_string(template, output="Error: URL rejected by security policy.")
                if "/pull/" in input_path:
                    final_output = process_github_pull_request(input_path)
                elif "/issues/" in input_path:
                    final_output = process_github_issue(input_path)
                else:
                    final_output = process_github_repo(input_path)
            elif parsed.scheme in ["http", "https"]:
                if not _is_safe_url(input_path):
                    return render_template_string(template, output="Error: URL rejected by security policy.")
                if "youtube.com" in input_path or "youtu.be" in input_path:
                    final_output = fetch_youtube_transcript(input_path)
                elif "arxiv.org" in input_path:
                    final_output = process_arxiv_pdf(input_path)
                else:
                    crawl_result = crawl_and_extract_text(input_path, max_depth=2, include_pdfs=True, ignore_epubs=True)
                    final_output = crawl_result['content']
                    with open(urls_list_file, 'w', encoding='utf-8') as urls_file:
                        urls_file.write('\n'.join(crawl_result['processed_urls']))
            elif (input_path.startswith("10.") and "/" in input_path) or input_path.isdigit():
                final_output = process_doi_or_pmid(input_path)
            else:
                # Reject local path processing from the web app (C2: SSRF/path traversal)
                return render_template_string(template, output="Error: Local path processing is not allowed via the web interface.")

            # Write the uncompressed output
            with open(output_file, "w", encoding="utf-8") as file:
                file.write(final_output)

            # Process the compressed output
            preprocess_text(output_file, processed_file)

            compressed_text = safe_file_read(processed_file)
            compressed_token_count = get_token_count(compressed_text)

            uncompressed_text = safe_file_read(output_file)
            uncompressed_token_count = get_token_count(uncompressed_text)

            # Copy to clipboard
            pyperclip.copy(uncompressed_text)

            return render_template_string(template,
                                          output=final_output,
                                          uncompressed_token_count=uncompressed_token_count,
                                          compressed_token_count=compressed_token_count)
        except Exception as e:
            return render_template_string(template, output=f"Error: {str(e)}")

    return render_template_string(template)


@app.route("/download")
def download():
    filename = request.args.get("filename")
    if not filename:
        abort(404)

    # C1 fix: Only allow downloading specific output files from the output directory
    basename = os.path.basename(filename)
    if basename not in ALLOWED_DOWNLOAD_FILES:
        abort(403)

    safe_path = os.path.join(OUTPUT_DIR, basename)
    if not os.path.isfile(safe_path):
        abort(404)

    return send_file(safe_path, as_attachment=True)

if __name__ == "__main__":
    # C3 fix: Never use debug=True in production; bind to localhost only
    app.run(debug=False, host="127.0.0.1", port=5000)
