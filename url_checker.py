import os
import sys
import re
import subprocess
import threading
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

DELIMITER = "___CURL_META_DELIMITER___"

def detect_soft_error(body: str) -> int:
    """Scans HTTP response body for error keywords when HTTP status returns 200 OK."""
    if not body:
        return 0
        
    clean_text = " ".join(body.split()).lower()
    
    title_match = re.search(r"<title[^>]*>(.*?)</title>", clean_text, re.IGNORECASE)
    title_text = title_match.group(1) if title_match else ""
    
    soft_404_kw = ["404 not found", "page not found", "404 error", "error 404", "page cannot be found", "does not exist"]
    soft_403_kw = ["403 forbidden", "access denied", "403 error", "forbidden: you don't have permission"]
    soft_500_kw = ["500 internal server error", "internal server error", "500 error", "something went wrong on our end"]

    for kw in soft_404_kw:
        if kw in title_text or kw in clean_text[:2000]:
            return 404
    for kw in soft_403_kw:
        if kw in title_text or kw in clean_text[:2000]:
            return 403
    for kw in soft_500_kw:
        if kw in title_text or kw in clean_text[:2000]:
            return 500
            
    return 0


def lookup_hosting_provider(ip: str, server_header: str) -> str:
    """Resolves IP address to ISP / Hosting Provider using IP API with Server header fallback."""
    if not ip or ip in ["0.0.0.0", "127.0.0.1", ""]:
        return server_header if server_header else "Unknown"

    try:
        # Free API endpoint for IP to ISP / Org resolution
        url = f"http://ip-api.com/json/{ip}?fields=status,isp,org,as,hosting"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "success":
                provider = data.get("org") or data.get("isp") or data.get("as")
                return provider
    except Exception:
        pass

    return server_header if server_header else "Unknown"


class URLCheckerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("URL Status & Redirect Checker")
        self.root.geometry("850x600")
        self.root.resizable(True, True)

        self.input_file_path = ""
        self.create_widgets()

    def create_widgets(self):
        # Top Frame - File Selection
        file_frame = tk.Frame(self.root, padx=10, pady=10)
        file_frame.pack(fill=tk.X)

        self.btn_select = tk.Button(
            file_frame, 
            text="Upload CSV / XLSX File", 
            command=self.select_file, 
            font=("Segoe UI", 10, "bold"), 
            bg="#2196F3", 
            fg="white", 
            padx=10, 
            pady=5
        )
        self.btn_select.pack(side=tk.LEFT)

        self.lbl_file = tk.Label(file_frame, text="No file selected", font=("Segoe UI", 9, "italic"), fg="gray", anchor="w")
        self.lbl_file.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)

        # Settings Frame
        settings_frame = tk.Frame(self.root, padx=10, pady=5)
        settings_frame.pack(fill=tk.X)

        tk.Label(settings_frame, text="Max Threads:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.spin_threads = tk.Spinbox(settings_frame, from_=1, to=50, width=5)
        self.spin_threads.delete(0, "end")
        self.spin_threads.insert(0, "10")
        self.spin_threads.pack(side=tk.LEFT, padx=5)

        self.btn_start = tk.Button(
            settings_frame, 
            text="Run Check", 
            command=self.start_processing, 
            font=("Segoe UI", 10, "bold"), 
            bg="#4CAF50", 
            fg="white", 
            padx=15, 
            pady=3,
            state=tk.DISABLED
        )
        self.btn_start.pack(side=tk.RIGHT)

        # Log Display Window
        log_frame = tk.Frame(self.root, padx=10, pady=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(log_frame, text="Execution Logs:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9), bg="#1E1E1E", fg="#D4D4D4")
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def select_file(self):
        file_path = filedialog.askopenfilename(
            title="Select URL File",
            filetypes=[("CSV and Excel Files", "*.csv *.xlsx *.xls"), ("CSV Files", "*.csv"), ("Excel Files", "*.xlsx *.xls")]
        )
        if file_path:
            self.input_file_path = file_path
            self.lbl_file.config(text=os.path.basename(file_path), fg="black")
            self.btn_start.config(state=tk.NORMAL)
            self.log(f"[*] Loaded file: {file_path}")

    def log(self, message: str):
        """Thread-safe logging function to append text to the log window."""
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.see(tk.END)

    def start_processing(self):
        if not self.input_file_path:
            messagebox.showerror("Error", "Please select a file first.")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.btn_select.config(state=tk.DISABLED)
        
        threading.Thread(target=self.run_checker, daemon=True).start()

    def run_curl_check(self, url: str, timeout: int = 10) -> dict:
        target_url = url if url.startswith(("http://", "https://")) else f"http://{url}"
        
        # Include %{primary_ip} in -w format string to extract host IP
        cmd = [
            "curl", "-s", "-i", "-L",
            "-w", f"\n{DELIMITER}\n%{{http_code}}|%{{time_total}}|%{{size_download}}|%{{num_redirects}}|%{{url_effective}}|%{{redirect_url}}|%{{primary_ip}}",
            "--max-time", str(timeout),
            target_url
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2, errors='replace')
            output = result.stdout
            
            if output and DELIMITER in output:
                raw_content, meta = output.rsplit(DELIMITER, 1)
                meta_parts = meta.strip().split("|")
                
                curl_final_code = int(meta_parts[0]) if meta_parts[0].isdigit() else 0
                resp_time = float(meta_parts[1]) if len(meta_parts) > 1 else 0.0
                size_bytes = int(meta_parts[2]) if len(meta_parts) > 2 and meta_parts[2].isdigit() else 0
                num_redirects = int(meta_parts[3]) if len(meta_parts) > 3 and meta_parts[3].isdigit() else 0
                effective_url = meta_parts[4] if len(meta_parts) > 4 else ""
                redirect_url = meta_parts[5] if len(meta_parts) > 5 else ""
                primary_ip = meta_parts[6] if len(meta_parts) > 6 else ""

                # Extract Server Header from response
                server_match = re.search(r"^Server:\s*(.*)$", raw_content, re.MULTILINE | re.IGNORECASE)
                server_header = server_match.group(1).strip() if server_match else "N/A"

                # Identify Hosting / Service Provider
                hosting_provider = lookup_hosting_provider(primary_ip, server_header)
                
                # Parse redirect chain status codes
                status_matches = re.findall(r"^HTTP/[\d\.]+\s+(\d{3})", raw_content, re.MULTILINE | re.IGNORECASE)
                status_codes = [int(c) for c in status_matches]
                
                if not status_codes:
                    status_codes = [curl_final_code]
                
                initial_status = status_codes[0]
                final_status = status_codes[-1]
                redirect_chain = " -> ".join(map(str, status_codes)) if len(status_codes) > 1 else str(initial_status)
                
                header_matches = list(re.finditer(r"^HTTP/[\d\.]+\s+\d{3}.*?(?=\r?\n\r?\n)", raw_content, re.DOTALL | re.MULTILINE | re.IGNORECASE))
                if header_matches:
                    body = raw_content[header_matches[-1].end():].strip()
                else:
                    body = raw_content.strip()
                
                redirect_target = effective_url if num_redirects > 0 else (redirect_url if redirect_url else "")
                
                # Soft Error Detection
                soft_code = detect_soft_error(body) if final_status == 200 else 0
                
                if final_status >= 400:
                    detected_status = f"{final_status} Error Page"
                elif soft_code > 0:
                    detected_status = f"{soft_code} (Soft {soft_code} Page - Returned 200 Header)"
                elif final_status == 200:
                    detected_status = "200 OK"
                else:
                    detected_status = str(final_status)
                    
                has_body = bool(body and len(body.strip()) > 0)
                if initial_status in [301, 302, 307, 308] or num_redirects > 0:
                    resp_type = f"Redirect ({initial_status})"
                    if final_status >= 400:
                        resp_type += f" -> Error Page ({final_status})"
                    elif soft_code > 0:
                        resp_type += f" -> Soft {soft_code} Page"
                    else:
                        resp_type += " -> Success (200)"
                elif final_status >= 400:
                    resp_type = "Full Error Page" if has_body else "Status Header Only"
                elif soft_code > 0:
                    resp_type = f"Soft {soft_code} Error Page"
                else:
                    resp_type = "Success (200 OK)"

                is_error = final_status >= 400 or soft_code > 0 or final_status == 0

                return {
                    "URL": url,
                    "Initial Status": initial_status,
                    "Final Status": final_status,
                    "Detected Status": detected_status,
                    "Redirect Chain": redirect_chain,
                    "Redirect Target": redirect_target,
                    "Redirect Count": num_redirects,
                    "IP Address": primary_ip,
                    "Server Header": server_header,
                    "Hosting Provider": hosting_provider,
                    "Response Time (s)": round(resp_time, 3),
                    "Size (Bytes)": size_bytes,
                    "Response Type": resp_type,
                    "Error": "None" if final_status > 0 else "No response",
                    "Response Body": body if is_error else ""
                }
            else:
                return {
                    "URL": url, "Initial Status": 0, "Final Status": 0, "Detected Status": "Failed / No Response",
                    "Redirect Chain": "None", "Redirect Target": "", "Redirect Count": 0,
                    "IP Address": "", "Server Header": "N/A", "Hosting Provider": "Unknown",
                    "Response Time (s)": 0.0, "Size (Bytes)": 0, "Response Type": "Empty Response",
                    "Error": "Empty response", "Response Body": ""
                }
                
        except subprocess.TimeoutExpired:
            return {"URL": url, "Initial Status": 0, "Final Status": 0, "Detected Status": "Timeout", "Redirect Chain": "None", "Redirect Target": "", "Redirect Count": 0, "IP Address": "", "Server Header": "N/A", "Hosting Provider": "Unknown", "Response Time (s)": timeout, "Size (Bytes)": 0, "Response Type": "Timeout", "Error": "Timeout", "Response Body": ""}
        except Exception as e:
            return {"URL": url, "Initial Status": 0, "Final Status": 0, "Detected Status": "Error", "Redirect Chain": "None", "Redirect Target": "", "Redirect Count": 0, "IP Address": "", "Server Header": "N/A", "Hosting Provider": "Unknown", "Response Time (s)": 0.0, "Size (Bytes)": 0, "Response Type": "Exception", "Error": str(e), "Response Body": ""}

    def run_checker(self):
        try:
            max_workers = int(self.spin_threads.get())
            ext = os.path.splitext(self.input_file_path)[1].lower()
            
            if ext == ".csv":
                df = pd.read_csv(self.input_file_path)
            elif ext in [".xlsx", ".xls"]:
                df = pd.read_excel(self.input_file_path)
            else:
                messagebox.showerror("Error", "Unsupported file format.")
                return

            possible_cols = [c for c in df.columns if "url" in str(c).lower() or "link" in str(c).lower()]
            url_column = possible_cols[0] if possible_cols else df.columns[0]
            
            urls = df[url_column].dropna().unique().tolist()
            self.log(f"[*] Found {len(urls)} unique URLs using column '{url_column}'. Starting check...")
            self.log("=" * 65)

            results = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_url = {executor.submit(self.run_curl_check, url): url for url in urls}
                
                for future in as_completed(future_to_url):
                    res = future.result()
                    results.append(res)
                    
                    log_msg = f"[{res['Redirect Chain']}] | {res['Response Time (s)']}s | {res['URL']} | Provider: {res['Hosting Provider']}"
                    if res["Redirect Target"]:
                        log_msg += f"\n   └─► Redirect Target ➔ {res['Redirect Target']}"
                    if "Soft" in res["Detected Status"] or res["Final Status"] >= 400:
                        log_msg += f"\n   └─► Status: {res['Detected Status']}"
                    
                    self.log(log_msg)

            results_df = pd.DataFrame(results)
            columns_order = [
                "URL", "Initial Status", "Final Status", "Detected Status", 
                "Redirect Chain", "Redirect Target", "Redirect Count", 
                "IP Address", "Server Header", "Hosting Provider",
                "Response Time (s)", "Size (Bytes)", "Response Type", "Error", "Response Body"
            ]
            results_df = results_df[columns_order]

            save_path = filedialog.asksaveasfilename(
                title="Save Report As",
                defaultextension=".csv",
                filetypes=[("CSV File", "*.csv"), ("Excel File", "*.xlsx")],
                initialfile="url_report.csv"
            )

            if save_path:
                if save_path.endswith(".xlsx"):
                    results_df.to_excel(save_path, index=False)
                else:
                    results_df.to_csv(save_path, index=False)
                    
                self.log(f"\n[+] Detailed report successfully saved to:\n{save_path}")
                messagebox.showinfo("Success", f"Report saved successfully!\nLocation: {save_path}")
            else:
                self.log("\n[!] Report save cancelled by user.")

        except Exception as e:
            self.log(f"\n[ERROR] An error occurred: {str(e)}")
            messagebox.showerror("Error", str(e))
            
        finally:
            self.btn_start.config(state=tk.NORMAL)
            self.btn_select.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = URLCheckerApp(root)
    root.mainloop()