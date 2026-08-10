import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

# ANSI Color Codes for terminal highlighting
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[92m"   # 200 OK
COLOR_RED = "\033[91m"     # 403, 404, Critical Errors
COLOR_YELLOW = "\033[93m"  # 3xx / Redirects / Other 4xx
COLOR_MAGENTA = "\033[95m" # 5xx Server Errors
COLOR_GRAY = "\033[90m"    # Timeout / Connection Error
COLOR_CYAN = "\033[96m"    # Redirects & Info

DELIMITER = "___CURL_META_DELIMITER___"

def run_curl_check(url: str, timeout: int = 10) -> dict:
    """Executes curl command for a single URL; classifies whether an error page or status header only was returned."""
    target_url = url if url.startswith(("http://", "https://")) else f"http://{url}"
    
    cmd = [
        "curl", "-s", "-L",
        "-w", f"\n{DELIMITER}\n%{{http_code}}|%{{time_total}}|%{{size_download}}|%{{num_redirects}}|%{{url_effective}}|%{{redirect_url}}",
        "--max-time", str(timeout),
        target_url
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2, errors='replace')
        output = result.stdout
        
        if output and DELIMITER in output:
            body, meta = output.rsplit(DELIMITER, 1)
            body = body.strip()
            parts = meta.strip().split("|")
            
            status_code = int(parts[0]) if parts[0].isdigit() else 0
            resp_time = float(parts[1]) if len(parts) > 1 else 0.0
            size_bytes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            num_redirects = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            effective_url = parts[4] if len(parts) > 4 else ""
            redirect_url = parts[5] if len(parts) > 5 else ""
            
            # Determine redirect target
            redirect_target = ""
            if num_redirects > 0:
                redirect_target = effective_url
            elif redirect_url:
                redirect_target = redirect_url
            
            # Check response body presence for error status codes (>= 400)
            is_error = status_code >= 400 or status_code == 0
            has_body = bool(body and len(body.strip()) > 0)
            
            if status_code >= 400:
                response_type = "Full Error Page" if has_body else "Status Header Only"
            elif status_code == 0:
                response_type = "Connection Failed"
            else:
                response_type = "Success / Redirect"
            
            return {
                "URL": url,
                "Status Code": status_code,
                "Response Time (s)": round(resp_time, 3),
                "Size (Bytes)": size_bytes,
                "Redirect Target": redirect_target,
                "Redirect Count": num_redirects,
                "Response Type": response_type,
                "Error": "None" if status_code > 0 else "No response",
                "Response Body": body if is_error else ""
            }
        else:
            return {
                "URL": url, 
                "Status Code": 0, 
                "Response Time (s)": 0.0, 
                "Size (Bytes)": 0, 
                "Redirect Target": "",
                "Redirect Count": 0,
                "Response Type": "Empty Response",
                "Error": "Empty response",
                "Response Body": ""
            }
            
    except subprocess.TimeoutExpired:
        return {"URL": url, "Status Code": 0, "Response Time (s)": timeout, "Size (Bytes)": 0, "Redirect Target": "", "Redirect Count": 0, "Response Type": "Timeout", "Error": "Timeout", "Response Body": ""}
    except Exception as e:
        return {"URL": url, "Status Code": 0, "Response Time (s)": 0.0, "Size (Bytes)": 0, "Redirect Target": "", "Redirect Count": 0, "Response Type": "Exception", "Error": str(e), "Response Body": ""}

def format_status_code(status_code: int, has_redirect: bool = False) -> str:
    """Applies ANSI colors based on HTTP status and redirect status."""
    code_str = str(status_code)
    if has_redirect:
        return f"{COLOR_YELLOW}{code_str} REDIRECT{COLOR_RESET}"
    elif status_code == 200:
        return f"{COLOR_GREEN}{code_str} OK{COLOR_RESET}"
    elif status_code == 403:
        return f"{COLOR_RED}{code_str} FORBIDDEN{COLOR_RESET}"
    elif status_code == 404:
        return f"{COLOR_RED}{code_str} NOT FOUND{COLOR_RESET}"
    elif 300 <= status_code < 400:
        return f"{COLOR_YELLOW}{code_str} REDIRECT{COLOR_RESET}"
    elif 400 <= status_code < 500:
        return f"{COLOR_YELLOW}{code_str} CLIENT ERROR{COLOR_RESET}"
    elif status_code >= 500:
        return f"{COLOR_MAGENTA}{code_str} SERVER ERROR{COLOR_RESET}"
    else:
        return f"{COLOR_GRAY}{code_str} FAILED{COLOR_RESET}"

def print_error_page_snippet(url: str, status_code: int, response_type: str, body: str, max_chars: int = 300):
    """Prints a highlighted snippet of the error page or notes status header only."""
    if response_type == "Status Header Only" or not body:
        print(f"   {COLOR_GRAY}└─ [Server returned HTTP {status_code} Status Header Only - No Body Content]{COLOR_RESET}")
        return
    
    clean_body = " ".join(body.split())
    snippet = clean_body[:max_chars] + ("..." if len(clean_body) > max_chars else "")
    
    print(f"   {COLOR_CYAN}┌─ FULL ERROR PAGE RETURNED ({status_code}) for {url}:{COLOR_RESET}")
    print(f"   {COLOR_CYAN}│{COLOR_RESET} {snippet}")
    print(f"   {COLOR_CYAN}└{"─" * 60}{COLOR_RESET}")

def process_url_file(file_path: str, url_column: str = None, max_workers: int = 10, output_csv: str = "url_report.csv"):
    """Ingests file, checks URLs, logs error page status, and outputs report."""
    
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(file_path)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
    else:
        raise ValueError(f"Unsupported file format: '{ext}'. Please supply a CSV or XLSX file.")
    
    if not url_column:
        possible_cols = [c for c in df.columns if "url" in str(c).lower() or "link" in str(c).lower()]
        url_column = possible_cols[0] if possible_cols else df.columns[0]
        print(f"[*] Auto-detected URL column: '{url_column}'")

    urls = df[url_column].dropna().unique().tolist()
    print(f"[*] Found {len(urls)} unique URLs to check using up to {max_workers} threads...\n")

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(run_curl_check, url): url for url in urls}
        
        print(f"{'STATUS':<20} | {'TIME (s)':<10} | {'SIZE (B)':<10} | URL")
        print("-" * 75)
        
        for future in as_completed(future_to_url):
            res = future.result()
            results.append(res)
            
            has_redirect = res["Redirect Count"] > 0
            colored_status = format_status_code(res["Status Code"], has_redirect)
            print(f"{colored_status:<29} | {res['Response Time (s)']:<10} | {res['Size (Bytes)']:<10} | {res['URL']}")
            
            # Log redirect target to terminal
            if res["Redirect Target"]:
                print(f"   {COLOR_YELLOW}└─► Redirected ({res['Redirect Count']} hop/s) ➔ {res['Redirect Target']}{COLOR_RESET}")

            # Print error page snippet or status-only log for HTTP 4xx, 5xx, or failed connections
            if res["Status Code"] >= 400 or res["Status Code"] == 0:
                print_error_page_snippet(res["URL"], res["Status Code"], res["Response Type"], res["Response Body"])

    # Generate Summary Report
    results_df = pd.DataFrame(results)
    
    print("\n" + "=" * 60)
    print("                STATUS CODE SUMMARY REPORT")
    print("=" * 60)
    
    summary = results_df.groupby("Status Code").size().reset_index(name="Count")
    summary["Description"] = summary["Status Code"].apply(
        lambda code: "Forbidden" if code == 403 
        else "Not Found" if code == 404 
        else "Server Error" if code >= 500
        else "OK" if code == 200 
        else "Connection Failed" if code == 0 
        else "Other"
    )
    
    redirect_count = len(results_df[results_df["Redirect Target"] != ""])
    print(f" {COLOR_YELLOW}Total Redirects Logged: {redirect_count} URLs{COLOR_RESET}\n")

    for _, row in summary.iterrows():
        status = row["Status Code"]
        count = row["Count"]
        desc = row["Description"]
        color = COLOR_RED if status in [403, 404] else (COLOR_MAGENTA if status >= 500 else (COLOR_GREEN if status == 200 else COLOR_YELLOW))
        print(f" {color}Status {status:<4} ({desc}): {count} URLs{COLOR_RESET}")

    # Re-order columns for clarity in CSV report
    columns_order = [
        "URL", "Status Code", "Response Time (s)", "Size (Bytes)", 
        "Redirect Target", "Redirect Count", "Response Type", "Error", "Response Body"
    ]
    results_df = results_df[columns_order]

    # Save Detailed Report
    results_df.to_csv(output_csv, index=False)
    print(f"\n[+] Detailed report saved to: {os.path.abspath(output_csv)}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_file = sys.argv[1]
    else:
        input_file = "url.xlsx"

    if os.path.exists(input_file):
        process_url_file(input_file, max_workers=10)
    else:
        print(f"Error: File '{input_file}' not found. Usage: python url_checker.py <filename.csv/.xlsx>")