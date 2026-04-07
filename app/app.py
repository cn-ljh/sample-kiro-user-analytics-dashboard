import os
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import boto3
from datetime import datetime
import time
from config import *

# Hide deploy button
os.environ['STREAMLIT_SERVER_ENABLE_STATIC_SERVING'] = 'false'

# Page configuration
st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon=PAGE_ICON,
    layout=LAYOUT,
    initial_sidebar_state="collapsed"
)

# Initialize theme in session state
if 'theme' not in st.session_state:
    st.session_state.theme = 'light'

# Theme colors
theme_colors = {
    'light': {
        'bg': '#ffffff',
        'secondary_bg': '#f8f9fa',
        'text': '#1f2937',
        'border': '#e5e7eb',
        'accent': '#1f77b4'
    },
    'dark': {
        'bg': '#0e1117',
        'secondary_bg': '#1e2530',
        'text': '#fafafa',
        'border': '#374151',
        'accent': '#4dabf7'
    }
}

current_theme = theme_colors[st.session_state.theme]

# Modern UI styling with theme support
modern_style = f"""
    <style>
    #MainMenu {{visibility: hidden;}}
    footer {{visibility: hidden;}}
    [data-testid="stToolbar"] {{display: none;}}
    .stDeployButton {{display: none;}}
    [data-testid="stSidebar"] {{display: none;}}
    section[data-testid="stSidebar"] {{display: none;}}
    .stApp {{
        background-color: {current_theme['bg']};
        color: {current_theme['text']};
        font-size: 1.05rem;
    }}
    .main {{ padding: 1.5rem 2rem; }}
    [data-testid="stMetricValue"] {{
        font-size: 2.4rem; font-weight: 700; color: {current_theme['accent']};
    }}
    [data-testid="stMetricLabel"] {{
        font-size: 1rem; font-weight: 500; color: {current_theme['text']}; opacity: 0.8;
    }}
    [data-testid="stMetricDelta"] {{
        font-size: 0.9rem;
    }}
    .block-container {{ padding-top: 1rem; padding-bottom: 2rem; max-width: 100%; }}
    .stButton button {{
        border-radius: 8px; font-weight: 500; padding: 0.4rem 0.8rem;
        transition: all 0.2s ease; border: 1px solid {current_theme['border']};
        background-color: {current_theme['secondary_bg']}; color: {current_theme['text']};
    }}
    .stButton button:hover {{
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        border-color: {current_theme['accent']};
    }}
    .dashboard-title {{
        font-weight: 700; font-size: 3rem; margin: 0; padding: 0;
        color: {current_theme['text']};
    }}
    .dashboard-subtitle {{
        font-size: 1.1rem; color: {current_theme['text']}; opacity: 0.5; margin-top: 4px;
    }}
    h2 {{ font-weight: 600; font-size: 1.6rem; margin-top: 1.5rem; margin-bottom: 0.8rem; color: {current_theme['text']}; }}
    h3 {{ font-weight: 600; font-size: 1.3rem; color: {current_theme['text']}; }}
    h4 {{ font-weight: 600; font-size: 1.15rem; color: {current_theme['text']}; }}
    p, li, span, label, .stMarkdown {{ color: {current_theme['text']}; }}
    strong {{ color: {current_theme['text']}; }}
    [data-testid="stMarkdownContainer"] {{ color: {current_theme['text']}; }}
    [data-testid="stMarkdownContainer"] p {{ color: {current_theme['text']}; }}
    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3,
    [data-testid="stMarkdownContainer"] h4 {{ color: {current_theme['text']}; }}
    hr {{ margin: 1.5rem 0; border-color: {current_theme['border']}; opacity: 0.3; }}
    .streamlit-expanderHeader {{ background-color: {current_theme['secondary_bg']}; border-radius: 8px; font-weight: 500; }}
    [data-testid="stDataFrame"] {{ border-radius: 8px; overflow: hidden; }}
    div[data-testid="stPlotlyChart"] {{
        border-radius: 12px;
        overflow: hidden;
    }}
    </style>
"""
st.markdown(modern_style, unsafe_allow_html=True)

# --- Athena helpers ---

@st.cache_resource
def get_athena_client():
    return boto3.client('athena', region_name=AWS_REGION)

@st.cache_resource
def get_identity_store_client():
    return boto3.client('identitystore', region_name=AWS_REGION)

@st.cache_resource
def get_glue_client():
    return boto3.client('glue', region_name=AWS_REGION)

@st.cache_data(ttl=3600)
def resolve_table_name():
    """Auto-discover the table name from the Glue database.
    Falls back to GLUE_TABLE_NAME env var if set."""
    if GLUE_TABLE_NAME:
        return GLUE_TABLE_NAME
    try:
        client = get_glue_client()
        response = client.get_tables(DatabaseName=ATHENA_DATABASE, MaxResults=1)
        tables = response.get('TableList', [])
        if tables:
            return tables[0]['Name']
    except Exception:
        pass
    raise Exception(f"No tables found in Glue database '{ATHENA_DATABASE}'. "
                    "Run the Glue crawler first, or set GLUE_TABLE_NAME in .env.")

@st.cache_data(ttl=3600)
def get_username(userid):
    if not IDENTITY_STORE_ID:
        return userid
    try:
        client = get_identity_store_client()
        # Kiro logs may include Identity Store ID prefix (e.g. "d-xxxxx.xxxxx-uuid")
        # Strip the prefix to get the actual UserId for the API call
        lookup_id = userid
        if '.' in userid:
            lookup_id = userid.split('.', 1)[1]
        response = client.describe_user(IdentityStoreId=IDENTITY_STORE_ID, UserId=lookup_id)
        return response.get('UserName') or response.get('DisplayName') or \
               response.get('Emails', [{}])[0].get('Value') or userid
    except Exception:
        return userid

@st.cache_data(ttl=3600)
def get_usernames_batch(userids):
    return {uid: get_username(uid) for uid in userids}

def execute_athena_query(query):
    client = get_athena_client()
    workgroup = os.getenv('ATHENA_WORKGROUP', '')
    kwargs = dict(
        QueryString=query,
        QueryExecutionContext={'Database': ATHENA_DATABASE},
    )
    if workgroup:
        kwargs['WorkGroup'] = workgroup
    else:
        kwargs['ResultConfiguration'] = {'OutputLocation': ATHENA_OUTPUT_BUCKET}
    response = client.start_query_execution(**kwargs)
    qid = response['QueryExecutionId']
    while True:
        result = client.get_query_execution(QueryExecutionId=qid)
        status = result['QueryExecution']['Status']['State']
        if status in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
            break
        time.sleep(1)  # nosemgrep
    if status == 'SUCCEEDED':
        return qid
    error_msg = result['QueryExecution']['Status'].get('StateChangeReason', 'Unknown error')
    raise Exception(f"Query failed: {error_msg}")

@st.cache_data(ttl=300)
def fetch_data(query):
    client = get_athena_client()
    qid = execute_athena_query(query)
    result = client.get_query_results(QueryExecutionId=qid)
    columns = [col['Label'] for col in result['ResultSet']['ResultSetMetadata']['ColumnInfo']]
    rows = []
    for row in result['ResultSet']['Rows'][1:]:
        rows.append([field.get('VarCharValue', '') for field in row['Data']])
    return pd.DataFrame(rows, columns=columns)

# --- Theme helpers ---

def get_plotly_template():
    return 'plotly_dark' if st.session_state.theme == 'dark' else 'plotly_white'

def get_chart_colors():
    if st.session_state.theme == 'dark':
        return {'paper_bgcolor': '#0e1117', 'plot_bgcolor': '#0e1117',
                'font_color': '#ffffff', 'title_color': '#ffffff'}
    return {'paper_bgcolor': '#ffffff', 'plot_bgcolor': '#ffffff',
            'font_color': '#1f2937', 'title_color': '#1f2937'}

# Modern color palette
CHART_COLORS = ['#4361ee', '#3a0ca3', '#7209b7', '#f72585', '#4cc9f0',
                '#4895ef', '#560bad', '#b5179e', '#f77f00', '#06d6a0']

def apply_chart_theme(fig):
    colors = get_chart_colors()
    fc, tc = colors['font_color'], colors['title_color']
    fig.update_layout(
        template=get_plotly_template(),
        paper_bgcolor=colors['paper_bgcolor'], plot_bgcolor=colors['plot_bgcolor'],
        font=dict(color=fc, size=12, family="Inter, system-ui, sans-serif"),
        title=dict(font=dict(color=tc, size=15, family="Inter, system-ui, sans-serif"), x=0.5, xanchor='center'),
        legend=dict(font=dict(color=fc, size=11), bgcolor='rgba(0,0,0,0)', borderwidth=0),
        margin=dict(l=40, r=40, t=50, b=40),
        hoverlabel=dict(bgcolor=colors['paper_bgcolor'], font_size=12, font_color=fc),
    )
    fig.update_xaxes(
        title_font=dict(color=fc, size=11), tickfont=dict(color=fc, size=10),
        gridcolor='rgba(128,128,128,0.1)', showline=True, linewidth=1, linecolor='rgba(128,128,128,0.2)'
    )
    fig.update_yaxes(
        title_font=dict(color=fc, size=11), tickfont=dict(color=fc, size=10),
        gridcolor='rgba(128,128,128,0.1)', showline=False
    )
    fig.update_annotations(font=dict(color=tc, size=13, family="Inter, system-ui, sans-serif"))
    return fig

def safe_float(val, default=0.0):
    try:
        return float(val) if val and str(val).strip() not in ('', 'None') else default
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    try:
        return int(float(val)) if val and str(val).strip() not in ('', 'None') else default
    except (ValueError, TypeError):
        return default

# --- Main app ---

def main():
    # Header
    header_col1, header_col2, header_col3 = st.columns([6, 1, 0.5])
    with header_col1:
        st.markdown('<p class="dashboard-title">⚡ Kiro Users Report</p>', unsafe_allow_html=True)
        st.markdown('<p class="dashboard-subtitle">Usage metrics across your organization</p>', unsafe_allow_html=True)
    with header_col2:
        refresh = st.button("🔄 Refresh Data")
    with header_col3:
        theme_icon = "🌙" if st.session_state.theme == 'light' else "☀️"
        if st.button(theme_icon, help="Toggle theme"):
            st.session_state.theme = 'dark' if st.session_state.theme == 'light' else 'light'
            st.rerun()

    st.markdown("")
    if refresh:
        st.cache_data.clear()

    try:
        # Auto-discover table name from Glue database
        table_name = resolve_table_name()
        with st.expander("ℹ️ Metric Definitions", expanded=False):
            st.markdown("""
            **Date**: Date of the report activity.

            **UserId**: ID of the user for whom the activity is reported.

            **Client Type**: KIRO_IDE, KIRO_CLI, or PLUGIN.

            **Subscription Tier**: Kiro subscription plan — Pro, ProPlus, Power.

            **ProfileId**: Profile associated with the user activity.

            **Total Messages**: Number of messages sent to and from Kiro. Includes user prompts, tool calls, and Kiro responses.

            **Chat Conversations**: Number of conversations by the user during the day.

            **Credits Used**: Credits consumed from the user subscription plan during the day.

            **Overage Enabled**: Whether overage is enabled for this user.

            **Overage Cap**: Overage limit set by the admin when overage is enabled. If overage is not enabled, shows the maximum credits included for the subscription plan as a preset value.

            **Overage Credits Used**: Total number of overage credits used by the user, if overage is enabled.

            ---

            📖 **Learn more about Kiro metrics**: [Kiro Documentation - Monitor and Track](https://kiro.dev/docs/enterprise/monitor-and-track/)
            """)

        # ── Overall Metrics ──
        st.header("📈 Overall Metrics")

        query_overall = f"""
        SELECT
            COUNT(DISTINCT userid) as total_users,
            SUM(TRY_CAST(total_messages AS INTEGER)) as total_messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as total_conversations,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits,
            SUM(TRY_CAST(overage_credits_used AS DOUBLE)) as total_overage
        FROM {table_name}
        """
        df_overall = fetch_data(query_overall)

        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("Total Users", safe_int(df_overall['total_users'].iloc[0]),
                      help="Unique users who have used Kiro")
        with col2:
            st.metric("Total Messages", f"{safe_int(df_overall['total_messages'].iloc[0]):,}",
                      help="Total messages sent to Kiro")
        with col3:
            st.metric("Chat Conversations", f"{safe_int(df_overall['total_conversations'].iloc[0]):,}",
                      help="Total chat conversations initiated")
        with col4:
            st.metric("Credits Used", f"{safe_float(df_overall['total_credits'].iloc[0]):,.1f}",
                      help="Total credits consumed across all users and client types")
        with col5:
            st.metric("Overage Credits", f"{safe_float(df_overall['total_overage'].iloc[0]):,.1f}",
                      help="Total overage credits consumed")

        st.markdown("---")

        # ── Client Type Breakdown ──
        st.header("🖥️ Usage by Client Type")

        query_client = f"""
        SELECT
            client_type,
            COUNT(DISTINCT userid) as unique_users,
            SUM(TRY_CAST(total_messages AS INTEGER)) as total_messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as total_conversations,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits
        FROM {table_name}
        GROUP BY client_type
        ORDER BY total_messages DESC
        """
        df_client = fetch_data(query_client)
        for c in ['unique_users', 'total_messages', 'total_conversations']:
            df_client[c] = df_client[c].apply(safe_int)
        df_client['total_credits'] = df_client['total_credits'].apply(safe_float)

        col1, col2 = st.columns(2)
        with col1:
            fig_client_pie = px.pie(
                df_client, values='total_messages', names='client_type',
                title='Messages by Client Type', hole=0.45,
                color_discrete_sequence=CHART_COLORS
            )
            fig_client_pie.update_traces(textinfo='label+percent', textposition='outside',
                                         pull=[0.03] * len(df_client))
            apply_chart_theme(fig_client_pie)
            st.plotly_chart(fig_client_pie, use_container_width=True)

        with col2:
            fig_client_bar = px.bar(
                df_client, x='client_type', y='total_credits',
                title='Credits Used by Client Type',
                color='client_type', text='total_credits',
                color_discrete_sequence=CHART_COLORS,
                labels={'total_credits': 'Credits', 'client_type': 'Client Type'}
            )
            fig_client_bar.update_traces(texttemplate='%{text:.1f}', textposition='outside',
                                          marker_line_width=0)
            fig_client_bar.update_layout(showlegend=False, bargap=0.4)
            apply_chart_theme(fig_client_bar)
            st.plotly_chart(fig_client_bar, use_container_width=True)

        st.markdown("---")

        # ── Top 10 Users ──
        st.header("🏆 Top 10 Users by Messages")

        query_top_users = f"""
        SELECT
            userid,
            SUM(TRY_CAST(total_messages AS INTEGER)) as total_messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as total_conversations,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits
        FROM {table_name}
        GROUP BY userid
        ORDER BY total_messages DESC
        LIMIT 10
        """
        df_top = fetch_data(query_top_users)
        df_top['userid'] = df_top['userid'].str.replace("'", "").str.replace('"', '')
        df_top['total_messages'] = df_top['total_messages'].apply(safe_int)
        df_top['total_conversations'] = df_top['total_conversations'].apply(safe_int)
        df_top['total_credits'] = df_top['total_credits'].apply(safe_float)

        userids = df_top['userid'].tolist()
        umap = get_usernames_batch(userids)
        df_top['username'] = df_top['userid'].map(umap)

        col1, col2 = st.columns([2, 3])
        with col1:
            st.subheader("🥇 Leaderboard")
            for idx, row in df_top.iterrows():
                rank = idx + 1
                medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"{rank}."
                st.markdown(f"**{medal} {row['username']}** — {row['total_messages']:,} messages")

        with col2:
            fig_top = px.bar(
                df_top, x='username', y='total_messages',
                title='Messages Sent', color='total_messages',
                color_continuous_scale='Purples',
                labels={'total_messages': 'Messages', 'username': 'User'}
            )
            fig_top.update_traces(marker_line_width=0)
            fig_top.update_layout(xaxis_tickangle=-45, showlegend=False, height=400, coloraxis_showscale=False)
            apply_chart_theme(fig_top)
            st.plotly_chart(fig_top, use_container_width=True)

        st.markdown("---")

        # ── Daily Activity Trends ──
        st.header("📅 Daily Activity Trends")

        query_daily = f"""
        SELECT
            date,
            SUM(TRY_CAST(total_messages AS INTEGER)) as messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as conversations,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as credits,
            COUNT(DISTINCT userid) as active_users
        FROM {table_name}
        GROUP BY date
        ORDER BY date
        """
        df_daily = fetch_data(query_daily)
        df_daily['date'] = pd.to_datetime(df_daily['date'])
        df_daily['messages'] = df_daily['messages'].apply(safe_int)
        df_daily['conversations'] = df_daily['conversations'].apply(safe_int)
        df_daily['credits'] = df_daily['credits'].apply(safe_float)
        df_daily['active_users'] = df_daily['active_users'].apply(safe_int)

        fig_daily = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Total Messages', 'Chat Conversations', 'Credits Used', 'Active Users'),
            vertical_spacing=0.15, horizontal_spacing=0.15
        )
        fig_daily.add_trace(
            go.Scatter(x=df_daily['date'], y=df_daily['messages'],
                       mode='lines+markers', name='Messages',
                       line=dict(color='#4361ee', width=2.5), marker=dict(size=5)),
            row=1, col=1)
        fig_daily.add_trace(
            go.Scatter(x=df_daily['date'], y=df_daily['conversations'],
                       mode='lines+markers', name='Conversations',
                       line=dict(color='#f72585', width=2.5), marker=dict(size=5)),
            row=1, col=2)
        fig_daily.add_trace(
            go.Scatter(x=df_daily['date'], y=df_daily['credits'],
                       mode='lines+markers', name='Credits',
                       line=dict(color='#06d6a0', width=2.5), marker=dict(size=5)),
            row=2, col=1)
        fig_daily.add_trace(
            go.Scatter(x=df_daily['date'], y=df_daily['active_users'],
                       mode='lines+markers', name='Active Users',
                       line=dict(color='#f77f00', width=2.5), marker=dict(size=5)),
            row=2, col=2)
        fig_daily.update_layout(height=600, showlegend=False,
                                title=dict(text="Daily Activity Overview", x=0.5, xanchor='center'))
        apply_chart_theme(fig_daily)
        st.plotly_chart(fig_daily, use_container_width=True)

        st.markdown("---")

        # ── Daily Trends by Client Type ──
        st.header("📊 Daily Trends by Client Type")

        query_daily_client = f"""
        SELECT
            date,
            client_type,
            SUM(TRY_CAST(total_messages AS INTEGER)) as messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as conversations
        FROM {table_name}
        GROUP BY date, client_type
        ORDER BY date
        """
        df_dc = fetch_data(query_daily_client)
        df_dc['date'] = pd.to_datetime(df_dc['date'])
        df_dc['messages'] = df_dc['messages'].apply(safe_int)
        df_dc['conversations'] = df_dc['conversations'].apply(safe_int)

        col1, col2 = st.columns(2)
        with col1:
            fig_msg_client = px.line(
                df_dc, x='date', y='messages', color='client_type',
                title='Daily Messages by Client Type', markers=True,
                color_discrete_sequence=CHART_COLORS,
                labels={'messages': 'Messages', 'date': 'Date', 'client_type': 'Client'}
            )
            fig_msg_client.update_traces(line=dict(width=2.5), marker=dict(size=5))
            apply_chart_theme(fig_msg_client)
            st.plotly_chart(fig_msg_client, use_container_width=True)

        with col2:
            fig_conv_client = px.line(
                df_dc, x='date', y='conversations', color='client_type',
                title='Daily Conversations by Client Type', markers=True,
                color_discrete_sequence=CHART_COLORS[2:],
                labels={'conversations': 'Conversations', 'date': 'Date', 'client_type': 'Client'}
            )
            fig_conv_client.update_traces(line=dict(width=2.5), marker=dict(size=5))
            apply_chart_theme(fig_conv_client)
            st.plotly_chart(fig_conv_client, use_container_width=True)

        st.markdown("---")

        # ── Credits Analysis ──
        st.header("💰 Credits Analysis")

        query_credits_user = f"""
        SELECT
            userid,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits,
            SUM(TRY_CAST(overage_credits_used AS DOUBLE)) as total_overage,
            MAX(TRY_CAST(overage_cap AS DOUBLE)) as overage_cap,
            MAX(overage_enabled) as overage_enabled
        FROM {table_name}
        GROUP BY userid
        ORDER BY total_credits DESC
        """
        df_credits = fetch_data(query_credits_user)
        df_credits['userid'] = df_credits['userid'].str.replace("'", "").str.replace('"', '')
        df_credits['total_credits'] = df_credits['total_credits'].apply(safe_float)
        df_credits['total_overage'] = df_credits['total_overage'].apply(safe_float)
        df_credits['overage_cap'] = df_credits['overage_cap'].apply(safe_float)

        umap_credits = get_usernames_batch(df_credits['userid'].tolist())
        df_credits['username'] = df_credits['userid'].map(umap_credits)
        df_credits['combined_credits'] = df_credits['total_credits'] + df_credits['total_overage']

        col1, col2 = st.columns(2)
        with col1:
            fig_credits = px.bar(
                df_credits.head(15), x='username', y='combined_credits',
                title='Top 15 Users by Total Credits',
                color='combined_credits', color_continuous_scale='Sunset',
                labels={'combined_credits': 'Credits', 'username': 'User'}
            )
            fig_credits.update_traces(marker_line_width=0)
            fig_credits.update_layout(xaxis_tickangle=-45, showlegend=False, coloraxis_showscale=False)
            apply_chart_theme(fig_credits)
            st.plotly_chart(fig_credits, use_container_width=True)

        with col2:
            # Credits vs overage — credits_used is base plan, overage_credits_used is additional
            df_credits_summary = pd.DataFrame({
                'Category': ['Base Credits', 'Overage Credits'],
                'Amount': [
                    df_credits['total_credits'].sum(),
                    df_credits['total_overage'].sum()
                ]
            })
            fig_overage = px.pie(
                df_credits_summary, values='Amount', names='Category',
                title='Base vs Overage Credits', hole=0.45,
                color_discrete_sequence=['#4361ee', '#f72585']
            )
            fig_overage.update_traces(textinfo='label+percent', textposition='outside',
                                       pull=[0.03, 0.03])
            apply_chart_theme(fig_overage)
            st.plotly_chart(fig_overage, use_container_width=True)

        # Monthly credit usage by user table
        st.subheader("📅 Credit Usage by User by Month")

        query_credits_monthly = f"""
        SELECT
            userid,
            DATE_FORMAT(DATE_PARSE(date, '%Y-%m-%d'), '%Y-%m') as month,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as credits_used
        FROM {table_name}
        GROUP BY userid, DATE_FORMAT(DATE_PARSE(date, '%Y-%m-%d'), '%Y-%m')
        ORDER BY month, userid
        """
        df_credits_monthly = fetch_data(query_credits_monthly)
        df_credits_monthly['userid'] = df_credits_monthly['userid'].str.replace("'", "").str.replace('"', '')
        df_credits_monthly['credits_used'] = df_credits_monthly['credits_used'].apply(safe_float)

        umap_monthly = get_usernames_batch(df_credits_monthly['userid'].unique().tolist())
        df_credits_monthly['User'] = df_credits_monthly['userid'].map(umap_monthly)

        # Pivot: rows = users, columns = months
        df_pivot = df_credits_monthly.pivot_table(
            index='User', columns='month', values='credits_used',
            aggfunc='sum', fill_value=0
        )
        # Sort columns chronologically
        df_pivot = df_pivot[sorted(df_pivot.columns)]
        # Add a total column
        df_pivot['Total'] = df_pivot.sum(axis=1)
        df_pivot = df_pivot.sort_values('Total', ascending=False)
        # Round for display
        df_pivot = df_pivot.round(1)

        st.dataframe(df_pivot, use_container_width=True, height=400)

        st.markdown("---")

        # ── Subscription Tier Breakdown ──
        st.header("🎫 Subscription Tier Breakdown")

        query_tier = f"""
        SELECT
            subscription_tier,
            COUNT(DISTINCT userid) as unique_users,
            SUM(TRY_CAST(total_messages AS INTEGER)) as total_messages,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits
        FROM {table_name}
        GROUP BY subscription_tier
        ORDER BY total_messages DESC
        """
        df_tier = fetch_data(query_tier)
        df_tier['unique_users'] = df_tier['unique_users'].apply(safe_int)
        df_tier['total_messages'] = df_tier['total_messages'].apply(safe_int)
        df_tier['total_credits'] = df_tier['total_credits'].apply(safe_float)

        col1, col2 = st.columns(2)
        with col1:
            fig_tier_users = px.bar(
                df_tier, x='subscription_tier', y='unique_users',
                title='Users by Subscription Tier', color='subscription_tier',
                color_discrete_sequence=CHART_COLORS,
                labels={'unique_users': 'Users', 'subscription_tier': 'Tier'}
            )
            fig_tier_users.update_traces(marker_line_width=0)
            fig_tier_users.update_layout(showlegend=False, bargap=0.4)
            apply_chart_theme(fig_tier_users)
            st.plotly_chart(fig_tier_users, use_container_width=True)

        with col2:
            fig_tier_credits = px.bar(
                df_tier, x='subscription_tier', y='total_credits',
                title='Credits by Subscription Tier', color='subscription_tier',
                color_discrete_sequence=CHART_COLORS[3:],
                labels={'total_credits': 'Credits', 'subscription_tier': 'Tier'}
            )
            fig_tier_credits.update_traces(marker_line_width=0)
            fig_tier_credits.update_layout(showlegend=False, bargap=0.4)
            apply_chart_theme(fig_tier_credits)
            st.plotly_chart(fig_tier_credits, use_container_width=True)

        st.markdown("---")

        # ── User Engagement Analysis ──
        st.header("👥 User Engagement Analysis")

        query_users = f"""
        SELECT
            userid,
            SUM(TRY_CAST(total_messages AS INTEGER)) as total_messages,
            SUM(TRY_CAST(chat_conversations AS INTEGER)) as total_conversations,
            SUM(TRY_CAST(credits_used AS DOUBLE)) as total_credits
        FROM {table_name}
        GROUP BY userid
        ORDER BY total_messages DESC
        """
        df_users = fetch_data(query_users)
        df_users['userid'] = df_users['userid'].str.replace("'", "").str.replace('"', '')
        df_users['total_messages'] = df_users['total_messages'].apply(safe_int)
        df_users['total_conversations'] = df_users['total_conversations'].apply(safe_int)
        df_users['total_credits'] = df_users['total_credits'].apply(safe_float)

        umap_users = get_usernames_batch(df_users['userid'].tolist())
        df_users['username'] = df_users['userid'].map(umap_users)

        # User segmentation
        st.subheader("📊 User Segmentation")

        def categorize_user(row):
            if row['total_messages'] >= 100 or row['total_conversations'] >= 20:
                return 'Power Users'
            elif row['total_messages'] >= 20 or row['total_conversations'] >= 5:
                return 'Active Users'
            elif row['total_messages'] > 0:
                return 'Light Users'
            else:
                return 'Idle Users'

        df_users['category'] = df_users.apply(categorize_user, axis=1)
        category_counts = df_users['category'].value_counts()
        pie_data = pd.DataFrame({'Category': category_counts.index, 'Count': category_counts.values})

        color_map = {'Power Users': '#4361ee', 'Active Users': '#06d6a0',
                     'Light Users': '#f77f00', 'Idle Users': '#e63946'}
        colors = [color_map.get(cat, '#999999') for cat in pie_data['Category']]

        col1, col2 = st.columns([2, 1])
        with col1:
            fig_seg = go.Figure(data=[go.Pie(
                labels=pie_data['Category'], values=pie_data['Count'], hole=0.45,
                marker=dict(colors=colors, line=dict(color=current_theme['bg'], width=3)),
                textinfo='label+percent+value', textposition='auto',
                hovertemplate='<b>%{label}</b><br>Users: %{value}<br>%{percent}<extra></extra>'
            )])
            fig_seg.update_layout(title='User Distribution by Engagement Level', height=450,
                                  showlegend=True,
                                  legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.05))
            apply_chart_theme(fig_seg)
            st.plotly_chart(fig_seg, use_container_width=True)

        with col2:
            st.markdown("### Category Definitions")
            st.markdown("""
            **🚀 Power Users**
            100+ messages OR 20+ conversations

            **💼 Active Users**
            20+ messages OR 5+ conversations

            **🌱 Light Users**
            At least 1 message sent

            **😴 Idle Users**
            No activity recorded
            """)
            st.markdown("---")
            st.markdown("### Quick Stats")
            for _, row in pie_data.iterrows():
                pct = (row['Count'] / pie_data['Count'].sum() * 100)
                st.metric(row['Category'], f"{row['Count']} users", f"{pct:.1f}%")

        st.markdown("---")

        # ── User Activity Timeline ──
        st.subheader("📅 User Activity Timeline")

        query_activity = f"""
        SELECT
            userid,
            MAX(date) as last_active_date,
            MIN(date) as first_active_date,
            COUNT(DISTINCT date) as active_days
        FROM {table_name}
        GROUP BY userid
        """
        df_activity = fetch_data(query_activity)
        df_activity['userid'] = df_activity['userid'].str.replace("'", "").str.replace('"', '')
        df_activity['last_active_date'] = pd.to_datetime(df_activity['last_active_date'])
        df_activity['first_active_date'] = pd.to_datetime(df_activity['first_active_date'])
        df_activity['active_days'] = df_activity['active_days'].apply(safe_int)
        df_activity['days_since_last_active'] = (pd.Timestamp.now() - df_activity['last_active_date']).dt.days

        umap_act = get_usernames_batch(df_activity['userid'].tolist())
        df_activity['username'] = df_activity['userid'].map(umap_act)

        df_act_merged = df_activity.merge(
            df_users[['userid', 'category', 'total_messages', 'total_credits']],
            on='userid', how='left'
        )

        col1, col2 = st.columns(2)
        with col1:
            df_recent = df_act_merged.nsmallest(15, 'days_since_last_active')
            fig_last = px.bar(
                df_recent, y='username', x='days_since_last_active',
                title='Days Since Last Activity (Top 15 Recent)',
                color='days_since_last_active', color_continuous_scale='Tealgrn_r',
                orientation='h', labels={'days_since_last_active': 'Days Ago', 'username': 'User'}
            )
            fig_last.update_traces(marker_line_width=0)
            fig_last.update_layout(height=500, yaxis={'categoryorder': 'total ascending'}, coloraxis_showscale=False)
            apply_chart_theme(fig_last)
            st.plotly_chart(fig_last, use_container_width=True)

        with col2:
            df_most = df_act_merged.nlargest(15, 'active_days')
            fig_days = px.bar(
                df_most, y='username', x='active_days',
                title='Total Active Days (Top 15)',
                color='active_days', color_continuous_scale='Purples',
                orientation='h', labels={'active_days': 'Active Days', 'username': 'User'}
            )
            fig_days.update_traces(marker_line_width=0)
            fig_days.update_layout(height=500, yaxis={'categoryorder': 'total ascending'}, coloraxis_showscale=False)
            apply_chart_theme(fig_days)
            st.plotly_chart(fig_days, use_container_width=True)

        # Detailed table
        st.markdown("#### 📋 Detailed User Activity Table")
        df_display = df_act_merged[['username', 'category', 'last_active_date',
                                     'days_since_last_active', 'active_days',
                                     'total_messages', 'total_credits']].copy()
        df_display.columns = ['User', 'Category', 'Last Active', 'Days Ago',
                              'Active Days', 'Messages', 'Credits']
        df_display['Last Active'] = df_display['Last Active'].dt.strftime('%Y-%m-%d')
        df_display = df_display.sort_values('Days Ago')

        filter_col1, filter_col2, filter_col3 = st.columns(3)
        with filter_col1:
            cat_filter = st.multiselect('Filter by Category',
                                        options=['All'] + sorted(df_display['Category'].dropna().unique().tolist()),
                                        default=['All'])
        with filter_col2:
            rec_filter = st.selectbox('Filter by Recency',
                                      ['All Users', 'Active (Last 7 days)', 'Recent (Last 30 days)',
                                       'Inactive (30+ days)', 'Dormant (90+ days)'])
        with filter_col3:
            sort_by = st.selectbox('Sort by', ['Days Ago', 'Active Days', 'Messages', 'Credits'])

        df_f = df_display.copy()
        if 'All' not in cat_filter:
            df_f = df_f[df_f['Category'].isin(cat_filter)]
        if rec_filter == 'Active (Last 7 days)':
            df_f = df_f[df_f['Days Ago'] <= 7]
        elif rec_filter == 'Recent (Last 30 days)':
            df_f = df_f[df_f['Days Ago'] <= 30]
        elif rec_filter == 'Inactive (30+ days)':
            df_f = df_f[df_f['Days Ago'] > 30]
        elif rec_filter == 'Dormant (90+ days)':
            df_f = df_f[df_f['Days Ago'] > 90]
        df_f = df_f.sort_values(sort_by, ascending=(sort_by == 'Days Ago'))

        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Filtered Users", len(df_f))
        with m2:
            st.metric("Avg Days Since Active", f"{df_f['Days Ago'].mean():.1f}" if len(df_f) else "N/A")
        with m3:
            st.metric("Avg Active Days", f"{df_f['Active Days'].mean():.1f}" if len(df_f) else "N/A")
        with m4:
            st.metric("Active Last Week", len(df_f[df_f['Days Ago'] <= 7]))

        st.dataframe(df_f, use_container_width=True, height=400, hide_index=True)

        st.markdown("---")

        # ── User Engagement Funnel ──
        st.header("🔻 User Engagement Funnel")

        total_users = len(df_users)
        users_with_messages = len(df_users[df_users['total_messages'] > 0])
        users_with_convos = len(df_users[df_users['total_conversations'] > 0])
        active_users = len(df_users[df_users['total_messages'] >= 20])
        power_users = len(df_users[df_users['total_messages'] >= 100])

        funnel_data = pd.DataFrame({
            'Stage': ['All Users', 'Sent Messages', 'Had Conversations',
                      'Active Users (20+ msgs)', 'Power Users (100+ msgs)'],
            'Count': [total_users, users_with_messages, users_with_convos, active_users, power_users]
        })
        funnel_data['Percentage'] = (funnel_data['Count'] / max(total_users, 1) * 100).round(1)

        col1, col2 = st.columns([3, 2])
        with col1:
            fig_funnel = go.Figure(go.Funnel(
                y=funnel_data['Stage'], x=funnel_data['Count'],
                textposition="inside", textinfo="value+percent initial", opacity=0.9,
                marker={"color": ['#4361ee', '#3a0ca3', '#7209b7', '#f72585', '#4cc9f0'],
                        "line": {"width": 0}},
                connector={"line": {"color": 'rgba(128,128,128,0.2)', "width": 1}}
            ))
            fig_funnel.update_layout(title='User Engagement Funnel', height=500,
                                     margin=dict(l=20, r=20, t=60, b=20))
            apply_chart_theme(fig_funnel)
            st.plotly_chart(fig_funnel, use_container_width=True)

        with col2:
            st.subheader("📊 Funnel Metrics")
            for _, row in funnel_data.iterrows():
                st.metric(label=row['Stage'], value=f"{row['Count']} users",
                          delta=f"{row['Percentage']}% of total")
                st.markdown("")

            st.markdown("---")
            st.subheader("🔄 Conversion Rates")
            if total_users > 0:
                st.markdown(f"**Message Activation:** {users_with_messages / total_users * 100:.1f}%")
                if users_with_messages > 0:
                    st.markdown(f"**Conversation Rate:** {users_with_convos / users_with_messages * 100:.1f}%")
                    st.markdown(f"**Active Retention:** {active_users / users_with_messages * 100:.1f}%")
                if active_users > 0:
                    st.markdown(f"**Power User Growth:** {power_users / active_users * 100:.1f}%")

    except Exception as e:
        st.error(f"Error fetching data: {str(e)}")
        st.info("Please ensure:")
        st.markdown("""
        - AWS credentials are configured
        - Glue crawler has run successfully
        - Athena database and table exist
        - S3 bucket for Athena results is accessible
        """)

if __name__ == "__main__":
    main()
