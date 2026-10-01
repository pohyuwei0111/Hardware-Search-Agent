import streamlit as st
import extra_streamlit_components as stx
import asyncio
import pandas as pd
import serpapi
from openai import AsyncOpenAI
from firecrawl import Firecrawl
import json_repair
import re
import io
from openpyxl.styles import Alignment, Font

# ==========================================
# UI CONFIGURATION & CSS
# ==========================================
st.set_page_config(page_title="AI Shopping Agent", page_icon="🛍️", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    .product-card {
        background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 12px;
        padding: 24px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); margin-bottom: 16px; transition: box-shadow 0.2s ease-in-out;
    }
    .product-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
    .badge-budget { background-color: #e6f4ea; color: #1e8e3e; padding: 4px 10px; border-radius: 16px; font-weight: 600; font-size: 14px; }
    .badge-balanced { background-color: #e8f0fe; color: #1a73e8; padding: 4px 10px; border-radius: 16px; font-weight: 600; font-size: 14px; }
    .badge-premium { background-color: #fce8e6; color: #d93025; padding: 4px 10px; border-radius: 16px; font-weight: 600; font-size: 14px; }
    .card-title { font-size: 18px; font-weight: 700; color: #202124; margin: 12px 0 8px 0; }
    .card-price { font-size: 24px; font-weight: 600; color: #1a73e8; margin-bottom: 12px; }
    .card-specs { font-size: 14px; color: #5f6368; margin-bottom: 16px; line-height: 1.5; }
    .card-reasoning { background-color: #f8f9fa; padding: 12px; border-radius: 8px; font-size: 14px; color: #3c4043; border-left: 4px solid #1a73e8;}
    .stTextInput>div>div>input { border-radius: 24px; border: 1px solid #dadce0; padding: 12px 24px; font-size: 16px; }
    .stTextInput>div>div>input:focus { border-color: #1a73e8; box-shadow: 0 1px 6px rgba(26,115,232,0.3); }
</style>
""", unsafe_allow_html=True)

# ==========================================
# SESSION STATE & COOKIE MANAGER
# ==========================================
if "analyzed_data" not in st.session_state:
    st.session_state.analyzed_data = None
    st.session_state.all_parsed_data = None
    st.session_state.search_query = ""

# Initialize the Cookie Manager directly (No caching)
cookie_manager = stx.CookieManager()

# ==========================================
# SIDEBAR SETTINGS (ISOLATED PER USER)
# ==========================================
with st.sidebar:
    # Using an official Google static asset URL
    st.image("https://www.gstatic.com/lamda/images/gemini_sparkle_v002_d4735304ff6292a690345.svg", width=60)
    st.header("⚙️ Agent Settings")
    
    st.markdown("🔒 **Bring Your Own Keys (BYOK)**\nKeys are saved securely in your browser's local cache, never on our servers.")
    
    # Retrieve cookies (will return None if they don't exist yet)
    saved_serp = cookie_manager.get("serp_key") or ""
    saved_fc = cookie_manager.get("fc_key") or ""
    saved_gemini = cookie_manager.get("gemini_key") or ""
    
    serp_key = st.text_input("SerpApi Key", value=saved_serp, type="password")
    fc_key = st.text_input("Firecrawl Key", value=saved_fc, type="password")
    gemini_key = st.text_input("Gemini API Key", value=saved_gemini, type="password")
    
    if st.button("💾 Save Keys to Browser", use_container_width=True):
        # Add a unique 'key' argument to each setter to prevent Streamlit component collisions
        cookie_manager.set("serp_key", serp_key, max_age=2592000, key="set_serp")
        cookie_manager.set("fc_key", fc_key, max_age=2592000, key="set_fc")
        cookie_manager.set("gemini_key", gemini_key, max_age=2592000, key="set_gemini")
        st.success("Keys securely saved to your browser cache!")
    
    st.divider()
    AVAILABLE_DOMAINS = ["shopee.com.my", "lazada.com.my", "my.rs-online.com", "digikey.my", "cytron.io", "taobao.com"]
    selected_domains = st.multiselect("Select Domains to Target", AVAILABLE_DOMAINS, default=["shopee.com.my", "digikey.my", "my.rs-online.com"])

# ==========================================
# ASYNC AGENT BACKEND LOGIC
# ==========================================
async def relax_query(client, original_query, status_container):
    status_container.info(f"LLM Relaxing query for fallback: '{original_query}'")
    instruction = (
        f"The e-commerce search query '{original_query}' returned zero results. "
        "Broaden this query slightly by removing only the most restrictive, niche, or descriptive words. "
        "CRITICAL: KEEP the most important numerical specifications."
    )
    response = await client.chat.completions.create(
        model="gemini-3.5-flash-lite",
        messages=[{"role": "system", "content": "You are a search query optimizer. Output ONLY the refined search string."},
                  {"role": "user", "content": instruction}],
        temperature=0.1
    )
    return response.choices[0].message.content.strip().replace('"', '')

async def search_google_snippets_async(platform_domain, current_query, target_count, serp_client):
    search_query = f"site:{platform_domain} {current_query}"
    try:
        raw_results = await asyncio.to_thread(serp_client.search, engine="google", q=search_query, gl="my", num=20)
        results = dict(raw_results)
        if "error" in results: return []
        
        valid_links = []
        for r in results.get("organic_results", []):
            link = r.get('link', 'N/A')
            if platform_domain not in link or '/search' in link or '/catalog' in link or '/list' in link: continue
            valid_links.append({"title": r.get('title', 'N/A'), "url": link, "snippet": r.get('snippet', 'N/A')})
            if len(valid_links) == target_count: break
        return valid_links
    except Exception:
        return []

async def scrape_with_firecrawl_async(url, snippet, fc):
    try:
        result = await asyncio.to_thread(fc.scrape, url, formats=["markdown"])
        markdown = result.markdown if hasattr(result, 'markdown') and result.markdown else ""
        if not markdown: raise ValueError("Empty")
        return markdown
    except Exception:
        return f"Snippet Data Fallback:\n{snippet}"

async def process_single_domain(domain, query, target_count, client, serp_client, fc, status_container):
    current_query = query
    products = []
    
    for tier in range(1, 3):
        status_container.text(f"[{domain}] Tier {tier} Search: {current_query}")
        links = await search_google_snippets_async(domain, current_query, target_count, serp_client)
        if links:
            products = links
            break
        if tier == 2: break
        current_query = await relax_query(client, query, status_container)
        
    extracted = ""
    if products:
        status_container.success(f"[{domain}] Found {len(products)} products. Scraping specs...")
        extracted += f"\n--- {domain.upper()} PRODUCTS ---\n"
        scrape_tasks = [scrape_with_firecrawl_async(p['url'], p['snippet'], fc) for p in products]
        scraped_markdowns = await asyncio.gather(*scrape_tasks)
        
        for p, markdown_data in zip(products, scraped_markdowns):
            extracted += f"URL: {p['url']}\nContent:\n{markdown_data[:4000]}\n"
    else:
        status_container.error(f"[{domain}] 0 results found.")
        
    return extracted

async def run_shopping_agent(query, domains, keys, status_container):
    client = AsyncOpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key=keys['gemini'])
    fc = Firecrawl(api_key=keys['fc'])
    serp_client = serpapi.Client(api_key=keys['serp'])
    
    target_count = 10 // len(domains) if domains else 0
    
    domain_tasks = [process_single_domain(domain, query, target_count, client, serp_client, fc, status_container) for domain in domains]
    results = await asyncio.gather(*domain_tasks)
    
    all_extracted_text = "".join(results)
    if not all_extracted_text.strip():
        return None, None

    status_container.warning("Passing ALL combined domain data to Gemini Analyst Model...")
    
    system_prompt = f"""You are an Expert Hardware Procurement Analyst. Target user requirement: "{query}".
Rules:
1. Parse ALL provided products into structured data (platform, model, specs, price, link).
2. From all valid products, select exactly THREE distinct items for your final recommendation:
   - 'Budget': Most economical option.
   - 'Balanced': Best price-to-performance compromise.
   - 'Premium': Highest-end option.
3. Output strictly valid JSON containing exactly TWO keys:
   - "all_products": An array of ALL products scraped, with keys: platform, model, specs, price, link.
   - "top_3": An array of the 3 selected items, with keys: category, platform, model, specs, price, link, reasoning.
4. If a price is from a snippet fallback, flag it with an asterisk (e.g., *RM 45.00)."""

    try:
        response = await client.chat.completions.create(
            model="gemini-3.5-flash-lite", 
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Analyze these products:\n\n{all_extracted_text}"}],
            temperature=0.1, response_format={"type": "json_object"}
        )
        
        data = json_repair.loads(response.choices[0].message.content)
        top_3 = data.get('top_3', [])
        all_products = data.get('all_products', [])
        
        if not top_3 and isinstance(data, list):
            top_3 = data[:3]
            all_products = data
            
        return top_3, all_products
    except Exception as e:
        status_container.error(f"LLM Parsing Error: {e}")
        return None, None

# ==========================================
# EXCEL GENERATOR (IN-MEMORY)
# ==========================================
def generate_excel_bytes(data, sheet_name, column_widths):
    buffer = io.BytesIO()
    df = pd.DataFrame(data)
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        
        for col, width in column_widths.items():
            worksheet.column_dimensions[col].width = width

        for cell in worksheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            
        for row in worksheet.iter_rows(min_row=2, max_row=worksheet.max_row, min_col=1, max_col=worksheet.max_column):
            for cell in row:
                cell.alignment = Alignment(
                    horizontal='center' if cell.column in [1, 4] else 'left', 
                    vertical='center', 
                    wrap_text=True
                )
    return buffer.getvalue()

# ==========================================
# MAIN UI LAYOUT
# ==========================================
st.title("Hi there, what hardware are you looking for?")
search_input = st.text_input("Search for hardware, components, or modules...", placeholder="e.g. RTX 5050 laptop, 24V 20A Power Supply...")
search_btn = st.button("Search & Analyze", type="primary")

if search_btn:
    if not (serp_key and fc_key and gemini_key):
        st.error("⚠️ Please enter all API keys in the left sidebar to proceed.")
    elif not selected_domains:
        st.error("⚠️ Please select at least one domain to target.")
    elif not search_input:
        st.error("⚠️ Please enter a product to search for.")
    else:
        with st.status("Initializing Shopping Agent...", expanded=True) as status_box:
            keys = {'serp': serp_key, 'fc': fc_key, 'gemini': gemini_key}
            analyzed, all_parsed = asyncio.run(run_shopping_agent(search_input, selected_domains, keys, status_box))
            
            st.session_state.analyzed_data = analyzed
            st.session_state.all_parsed_data = all_parsed
            st.session_state.search_query = search_input
            
            status_box.update(label="Analysis Complete!", state="complete", expanded=False)

# RENDER RESULTS
if st.session_state.analyzed_data:
    st.markdown("### ✨ Curated Hardware Recommendations")
    cols = st.columns(3)
    
    for index, item in enumerate(st.session_state.analyzed_data):
        cat = item.get('category', 'Option').title()
        badge_class = f"badge-{cat.lower()}" if cat.lower() in ['budget', 'balanced', 'premium'] else "badge-balanced"
        platform_name = item.get('platform', 'Online Store')
        
        with cols[index % 3]:
            st.markdown(f"""
            <div class="product-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span class="{badge_class}">{cat}</span>
                    <span style="font-size: 12px; font-weight: 600; color: #5f6368; background-color: #f1f3f4; padding: 2px 8px; border-radius: 12px;">🏪 {platform_name}</span>
                </div>
                <div class="card-title">{item.get('model', 'Unknown Model')}</div>
                <div class="card-price">{item.get('price', 'N/A')}</div>
                <div class="card-specs">{item.get('specs', 'No specs available.')}</div>
                <div class="card-reasoning"><b>Analyst Note:</b> {item.get('reasoning', 'No reasoning provided.')}</div>
                <br>
                <a href="{item.get('link', '#')}" target="_blank" style="color: #1a73e8; text-decoration: none; font-weight: 600;">🔗 View on {platform_name} →</a>
            </div>
            """, unsafe_allow_html=True)
    
    st.divider()
    st.markdown("### 📥 Export Final Data")
    col1, col2 = st.columns(2)
    
    safe_filename = re.sub(r'[\\/*?:"<>| ]', '_', st.session_state.search_query.strip()).strip('_')
    
    excel_analysis = generate_excel_bytes(
        st.session_state.analyzed_data, 
        'Product Comparison', 
        {'A': 15, 'B': 20, 'C': 30, 'D': 50, 'E': 20, 'F': 45, 'G': 40}
    )
    col1.download_button(
        label="📊 Download Top 3 Comparative Analysis (.xlsx)",
        data=excel_analysis,
        file_name=f"{safe_filename}_top3_analysis.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )
    
    if st.session_state.all_parsed_data:
        excel_raw = generate_excel_bytes(
            st.session_state.all_parsed_data, 
            'All Scraped Products', 
            {'A': 20, 'B': 30, 'C': 60, 'D': 20, 'E': 50}
        )
        col2.download_button(
            label="🛠️️ Download ALL Structured Products (.xlsx)",
            data=excel_raw,
            file_name=f"{safe_filename}_all_products.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
        with st.expander("🔍 View All Scraped Products Data"):
            st.dataframe(pd.DataFrame(st.session_state.all_parsed_data), use_container_width=True)