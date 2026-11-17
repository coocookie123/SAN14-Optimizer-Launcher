// [W]三国志14优化启动器 - small native launcher for the SAN14 report-skip mod.
// It only shows the options and starts processes; all game patching is done by the Python helper
// (app\tools\report_button.py, run with the bundled, signed pythonw.exe).
//   game directory empty -> start through Steam (steam://rungameid/872410); Steam knows the install path
//   game directory set   -> run <dir>\SAN14_Launcher.exe (or the CE game exe) directly, for non-Steam installs
//   any option checked   -> pythonw -P report_button.py [--no-skip] [--area-bonus P,C] [--takeover] [--trap-near]
//                           [--ai id,id] [--hr-dll <path> [--hr-block commandbook]]
//                           (--steam | "<game exe>")
//   nothing checked      -> start the plain game
// Options (settings.ini [launcher]): skip_report, area_bonus + player_bonus / ai_bonus, takeover, trap_near
// (陷阱近身触发, default 0), ai_modules (comma list of app\ai\modules\<id>.json), game_dir, hr_dll (the user's own
// 人力资源管理器 dinput8.dll, loaded from where it is, never copied into the game directory), hr_block_commandbook
// (default 1).
#ifndef UNICODE
#define UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <wchar.h>

#define APP_TITLE L"[W]三国志14优化启动器"
#define APP_VERSION L"v0.8.0"
#define STEAM_URL L"steam://rungameid/872410"
#define AUTO_TEXT L"自动（通过 Steam 启动）"

enum {
    ID_SKIP = 101, ID_START = 102, ID_QUIT = 103, ID_PICK = 104, ID_CLEAR = 105,
    ID_BONUS = 110, ID_PLAYER = 111, ID_AI = 112, ID_PLAYER_UD = 113, ID_AI_UD = 114,
    ID_PRESET_HIGH = 120, ID_PRESET_SUPER = 121, ID_PRESET_ULTIMATE = 122, ID_TAKEOVER = 130,
    ID_HR_PICK = 150, ID_HR_CLEAR = 151, ID_HR_BLOCK_CB = 152, ID_TRAP = 160,
    ID_AIMOD0 = 140,                    // 140.. one checkbox per AI_MODULES entry
    ID_TIMER_CLOSE = 1
};
static const int BONUS_DEFAULT_PLAYER = 4, BONUS_DEFAULT_AI = 4;   // 4/4 = no extra bonus for the computer

static HINSTANCE g_inst;
static HWND g_wnd, g_skip, g_takeover, g_trap, g_start, g_status, g_dirText, g_clear;
static HWND g_bonus, g_player, g_ai, g_playerUd, g_aiUd, g_bonusLabels[3], g_presets[3];
static HWND g_heads[7], g_hints[6];
static const wchar_t TRAP_TIP[] =
    L"对所有陷阱类设施生效（陷阱、落石，以及火攻的猛火陷阱、火陷阱、火船，游戏里用的是同一个判定）。"
    L"原版的陷阱、落石、猛火陷阱只要有敌军走进离它 2 格以内就发动，常常是打头的一支刚碰到范围边缘就触发，"
    L"后面的部队还没进效果范围，陷阱白白用掉。勾上后敌军要走到陷阱那一格或紧挨它的 6 格才发动；"
    L"发动后的伤害、状态异常和效果范围（2 格，加政策的范围加成）都照原版。火陷阱、火船本来就是 1 格，不变。"
    L"我方和电脑的陷阱一样处理，设施说明里的「发动范围」也显示 1。"
    L"由同一个 DLL 在内存里改，不改游戏文件；勾上后游戏由本工具直接启动（和电脑 AI 一样）。";
static HWND g_hrText, g_hrClear, g_hrBlockCb;
static wchar_t g_hrDll[MAX_PATH];

// computer-AI modules (behaviour-tree patches, app\ai\modules\<id>.json), applied when the game starts
struct AiModule { const wchar_t* id; const wchar_t* label; const wchar_t* tip; };
static const AiModule AI_MODULES[] = {
    {L"deploy", L"出兵编成",
     L"迎击时几名强将平分兵力（最多 3 人），会锋矢的当侧翼，统率低的带 2000 兵、用雁行当啦啦队。"
     L"只管迎击的编成，攻城出多少兵见「攻城评估」；出不出兵、打谁照原版。"},
    {L"rally", L"集结齐进",
     L"攻城时同一座城出来的主力先集结，别处来的增援自己追；攻城兵器跟着本队走，到城外再等它们到齐一起攻城。"
     L"几个军势打同一座城时，近处的晚点出发、先到的在城外等，到齐了一起攻。"
     L"迎击时落后的部队往本队追，本队原地等，跑在前面的也停下等，不往回拉。"
     L"攻城路上跑在前面、身边没有友军的部队，离目标城 5 到 8 格时原地等后面打同一座城的友军；"
     L"等的时候敌军先逼近就退到友军旁边，每座城最多等 8 天。"},
    {L"engage", L"会战协同",
     L"两支以上部队的军势遇敌时先会合（已经贴着敌军的不拉走），到齐后一起上。交战中每天分配攻击目标："
     L"贴着敌军的打贴着的那支，其余先打近的；一支敌军够打了就去打下一支，锋矢优先打敌方远程和攻城兵器。"
     L"雁行跟在本队后面。攻城时不往回拉已经靠近城的部队（攻城集结交给集结齐进）。"
     L"近战部队被敌军贴上、自己的命令却是原地待命时，改成打贴身的那支敌军，身边没敌军了再恢复待命。"},
    {L"retreat", L"野战撤退",
     L"迎击时，附近敌军战力明显强过我方，就整个军势撤回本队出发的城，所有部队回同一座城。攻城不撤退；"
     L"还在自家城边（4 格内）时不撤。我方战力按 8 格内所有友军算（和敌方同一范围），"
     L"一个军势要撤时，8 格内其他迎击军势一起撤，不会一拨撤一拨打。"},
    {L"stance", L"雁行站位",
     L"雁行是远程阵型：离敌三四格时先走到离敌正好两格的位置（优先友军身后），两格时停下放箭；被贴上就后撤，尽量撤到友军身后。"
     L"每天调整，由同一个 DLL 在内存里做，不改游戏文件。"},
    {L"walls", L"城下迎击",
     L"敌军已经围到自家城下时照样考虑出城迎击。原版在顶级难度下，对方势力实力超过我方 3 倍、君主野心不高就整轮不迎击，"
     L"小势力被围时城里兵再多也不出城，任由攻城兵器拆城。只放开城下这一种情况，兵力够不够出城仍按原版判断。"
     L"援军打完一波不急着回城：附近另一座自家城还有敌军以它为目标赶来时，能打就去打来敌"
     L"（交战处我方兵力够敌方六成，或来敌已到身边、城边），打不过就进那座城加强守备；"
     L"城没威胁了、守满 20 天或兵少了再照原版回城。"
     L"城边没在交战的近战部队优先去打敌方攻城兵器；正要回城的部队发现城边有敌军、我方兵力够敌军六成，就留在城外打，不进城。"
     L"敌军在水面时：迎击判断把水上的敌军战力按一半算，守方更容易出城；出城的部队到岸边等它、隔岸打，不下水追。"
     L"敌军靠近我方港口水域时，按游戏的伤害公式比下水打、岸边打、等它上岸三种打法，挑最划算的；"
     L"预计都吃亏的不去，能去的兵不到敌军一半也不去。由同一个 DLL 每天调整。"},
    {L"siegeplan", L"攻城评估",
     L"电脑攻城时按守方的真实守力决定什么时候打：目标城附近守方城可能派来的援军按实际赶来的比例算进去"
     L"（邻城的兵扣 5000，18 格内算八成、30 格内五成、45 格内一成，合计最多 45000），守方在自家地盘的府加成也算进去"
     L"（一般多算两三成）。守军多、援军近的城要攒够兵才打，打的时候带的兵更多。出兵那一刻，出兵城的留守线降到六成"
     L"（例如 5000 → 3000），稍远的自家城也可以一起出兵。打哪座城照原版。"
     L"到了城外 7 到 10 格、附近没有敌军时主力先停下，派兵少的近战部队去踩城周围没人守的敌方府，"
     L"等府易主（下一旬开头）、后面的部队到齐再一起攻城，最多等 15 天；已经和敌军贴着的部队照打不停。"
     L"周围没有要占的府就不停，只等同城的自家部队到齐。踩完府的部队回主力停下的地方一起等；城打得到的 3 格以内不派人、"
     L"不去踩府，已经到城下的部队就跟着攻城，不在城的射程里干等。"
     L"攻城途中每天看粮道：有敌军从身后靠近粮道（或粮道已经断了）时，派兵够的近战部队回头打它，敌军多就附近的部队一起打；"
     L"粮道经过的没人守的敌方府，派近处移动快的部队去踩掉，免得府自己把粮道涂回去。护粮和踩府的兵合计不超过攻城部队的一半。"
     L"攻城出兵凑不够目标战力时这一次就不出，等下一旬兵够了再打（原版在命令书用完时会硬派出去；这一条对所有电脑势力生效）。"
     L"「三国志14 优化伴侣」的电脑出兵类选项也改攻城出兵，同时开援军可能算两次，建议只开一边。"},
    {L"gate", L"守关",
     L"敌军攻打电脑势力的关隘时，关后只留雁行：雁行走到关这一侧射得到攻方的位置放箭，射击位先到先得，"
     L"没有射击位就守在关口；近战隔着关打不到，送回最近的自家城，不在关后排长队。附近的城为守关迎击时，"
     L"会雁行的武将用雁行出城（每座关最多 3 支），其余不出。守关的雁行不被别的命令带出关外。"
     L"和野战撤退一起开时，撤退判断不把隔着关走不过来的敌军算进去。由同一个 DLL 每天调整。"},
};
static const int AI_COUNT = sizeof(AI_MODULES) / sizeof(AI_MODULES[0]);
static HWND g_aiMods[AI_COUNT], g_tip;
static HFONT g_font, g_fontBold, g_fontSmall, g_fontHead;
static wchar_t g_dir[MAX_PATH], g_ini[MAX_PATH], g_gameDir[MAX_PATH];
static UINT g_dpi = 96;

static int S(int v) { return MulDiv(v, (int)g_dpi, 96); }

static bool Exists(const wchar_t* p) { return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES; }

// first existing path of exe_dir\<candidate>
static bool FindRel(wchar_t* out, const wchar_t* const* cands, int n) {
    for (int i = 0; i < n; i++) {
        wchar_t p[MAX_PATH];
        wsprintfW(p, L"%s\\%s", g_dir, cands[i]);
        wchar_t full[MAX_PATH];
        if (GetFullPathNameW(p, MAX_PATH, full, nullptr) && Exists(full)) { lstrcpyW(out, full); return true; }
    }
    return false;
}

static bool FindPython(wchar_t* out) {
    const wchar_t* c[] = {L"runtime\\pythonw.exe", L"..\\runtime\\pythonw.exe"};
    if (FindRel(out, c, 2)) return true;
    wchar_t la[MAX_PATH];
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", la, MAX_PATH)) {   // Microsoft Store Python alias
        wsprintfW(out, L"%s\\Microsoft\\WindowsApps\\pythonw.exe", la);
        if (Exists(out)) return true;
    }
    return SearchPathW(nullptr, L"pythonw.exe", nullptr, MAX_PATH, out, nullptr) > 0;
}

static bool FindScript(wchar_t* out) {
    const wchar_t* c[] = {L"app\\tools\\report_button.py", L"tools\\report_button.py", L"..\\tools\\report_button.py"};
    return FindRel(out, c, 3);
}

// Program to start inside a game directory: the game's own launcher, else the CE game exe.
static bool GameExeIn(const wchar_t* dir, wchar_t* out) {
    const wchar_t* names[] = {L"SAN14_Launcher.exe", L"SAN14CE_SC.exe", L"SAN14CE_TC.exe", L"SAN14CE.exe", L"SAN14CE_KO.exe"};
    for (const wchar_t* n : names) {
        wsprintfW(out, L"%s\\%s", dir, n);
        if (Exists(out)) return true;
    }
    return false;
}

static bool SteamInstalled() {
    wchar_t v[MAX_PATH];
    DWORD n = sizeof(v);
    return RegGetValueW(HKEY_CURRENT_USER, L"Software\\Valve\\Steam", L"SteamExe", RRF_RT_REG_SZ, nullptr, v, &n) == ERROR_SUCCESS ||
           (n = sizeof(v), RegGetValueW(HKEY_LOCAL_MACHINE, L"SOFTWARE\\WOW6432Node\\Valve\\Steam", L"InstallPath",
                                        RRF_RT_REG_SZ, nullptr, v, &n) == ERROR_SUCCESS);
}

static void SetStatus(const wchar_t* s) { SetWindowTextW(g_status, s); }

static void ShowGameDir() {
    SetWindowTextW(g_dirText, g_gameDir[0] ? g_gameDir : AUTO_TEXT);
    EnableWindow(g_clear, g_gameDir[0] != 0);
}

static void PickGameDir() {
    IFileOpenDialog* dlg = nullptr;
    if (FAILED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dlg)))) return;
    DWORD opts = 0;
    dlg->GetOptions(&opts);
    dlg->SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
    dlg->SetTitle(L"选择三国志14 的安装目录（里面有 SAN14_Launcher.exe）");
    if (SUCCEEDED(dlg->Show(g_wnd))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item))) {
            wchar_t* path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
                wchar_t exe[MAX_PATH];
                if (GameExeIn(path, exe)) {
                    lstrcpynW(g_gameDir, path, MAX_PATH);
                    WritePrivateProfileStringW(L"launcher", L"game_dir", g_gameDir, g_ini);
                    ShowGameDir();
                    SetStatus(L"");
                } else {
                    MessageBoxW(g_wnd, L"这个目录里没有找到三国志14 的程序（SAN14_Launcher.exe 或 SAN14CE_SC.exe）。\n"
                                       L"请选择游戏的安装目录。", APP_TITLE, MB_ICONWARNING);
                }
                CoTaskMemFree(path);
            }
            item->Release();
        }
    }
    dlg->Release();
}

// hr_dll may be relative to this launcher's folder (e.g. 第三方Mod\人力资源管理器\dinput8.dll), so the copy kept
// inside the W folder still works after the folder is moved
static void HrDllPath(wchar_t* out) {
    if (!g_hrDll[0]) { out[0] = 0; return; }
    if (g_hrDll[1] == L':' || (g_hrDll[0] == L'\\' && g_hrDll[1] == L'\\')) lstrcpynW(out, g_hrDll, MAX_PATH);
    else wsprintfW(out, L"%s\\%s", g_dir, g_hrDll);
}

static void ShowHrDll() {
    wchar_t full[2 * MAX_PATH];
    HrDllPath(full);
    SetWindowTextW(g_hrText, g_hrDll[0] ? full : L"未设置（不加载）");
    EnableWindow(g_hrClear, g_hrDll[0] != 0);
}

// 人力资源管理器: the user's own copy of its dinput8.dll, outside the game directory (it is loaded from there)
static void PickHrDll() {
    IFileOpenDialog* dlg = nullptr;
    if (FAILED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dlg)))) return;
    DWORD opts = 0;
    dlg->GetOptions(&opts);
    dlg->SetOptions(opts | FOS_FORCEFILESYSTEM | FOS_FILEMUSTEXIST);
    COMDLG_FILTERSPEC types[] = {{L"DLL", L"*.dll"}};
    dlg->SetFileTypes(1, types);
    dlg->SetTitle(L"选择人力资源管理器的 dinput8.dll（放在游戏目录以外）");
    if (SUCCEEDED(dlg->Show(g_wnd))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item))) {
            wchar_t* path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
                wchar_t dir[MAX_PATH], exe[MAX_PATH];
                lstrcpynW(dir, path, MAX_PATH);
                wchar_t* slash = wcsrchr(dir, L'\\');
                if (slash) *slash = 0;
                if (GameExeIn(dir, exe)) {
                    MessageBoxW(g_wnd, L"这份 DLL 在游戏目录里，游戏启动时自己就会加载它。\n"
                                       L"请把它挪到游戏目录以外（例如本工具的目录），再重新选择。", APP_TITLE, MB_ICONWARNING);
                } else {
                    int n = lstrlenW(g_dir);           // inside this launcher's folder: keep it relative
                    if (CompareStringOrdinal(path, n, g_dir, n, TRUE) == CSTR_EQUAL && path[n] == L'\\')
                        lstrcpynW(g_hrDll, path + n + 1, MAX_PATH);
                    else
                        lstrcpynW(g_hrDll, path, MAX_PATH);
                    WritePrivateProfileStringW(L"launcher", L"hr_dll", g_hrDll, g_ini);
                    ShowHrDll();
                    SetStatus(L"");
                }
                CoTaskMemFree(path);
            }
            item->Release();
        }
    }
    dlg->Release();
}

static bool Checked(HWND h) { return SendMessageW(h, BM_GETCHECK, 0, 0) == BST_CHECKED; }

static int ClampBonus(int v) { return v < 0 ? 0 : v > 100 ? 100 : v; }

static int GetBonus(HWND ud) {
    BOOL err = FALSE;
    int v = (int)SendMessageW(ud, UDM_GETPOS32, 0, (LPARAM)&err);
    return ClampBonus(v);
}

static void SetBonus(HWND ud, int v) { SendMessageW(ud, UDM_SETPOS32, 0, ClampBonus(v)); }

static void WriteInt(const wchar_t* key, int v) {
    wchar_t s[16];
    wsprintfW(s, L"%d", v);
    WritePrivateProfileStringW(L"launcher", key, s, g_ini);
}

// "a,b,c" contains id as a whole item
static bool InList(const wchar_t* list, const wchar_t* id) {
    int n = lstrlenW(id);
    for (const wchar_t* p = list; (p = wcsstr(p, id)) != nullptr; p += n)
        if ((p == list || p[-1] == L',') && (p[n] == 0 || p[n] == L',')) return true;
    return false;
}

// checked AI modules as "a,b,c" (empty: none)
static void AiList(wchar_t* out) {
    out[0] = 0;
    for (int i = 0; i < AI_COUNT; i++) {
        if (SendMessageW(g_aiMods[i], BM_GETCHECK, 0, 0) != BST_CHECKED) continue;
        if (out[0]) lstrcatW(out, L",");
        lstrcatW(out, AI_MODULES[i].id);
    }
}

static void UpdateBonusEnabled() {
    BOOL on = Checked(g_bonus);
    HWND hs[] = {g_player, g_ai, g_playerUd, g_aiUd, g_bonusLabels[0], g_bonusLabels[1], g_bonusLabels[2],
                 g_presets[0], g_presets[1], g_presets[2]};
    for (HWND h : hs) EnableWindow(h, on);
}

// remember the options (also when the window is closed without starting the game)
static void SaveSettings() {
    if (!g_skip) return;
    WritePrivateProfileStringW(L"launcher", L"skip_report", Checked(g_skip) ? L"1" : L"0", g_ini);
    WritePrivateProfileStringW(L"launcher", L"area_bonus", Checked(g_bonus) ? L"1" : L"0", g_ini);
    WritePrivateProfileStringW(L"launcher", L"takeover", Checked(g_takeover) ? L"1" : L"0", g_ini);
    WritePrivateProfileStringW(L"launcher", L"trap_near", Checked(g_trap) ? L"1" : L"0", g_ini);
    wchar_t ai[256];
    AiList(ai);
    WritePrivateProfileStringW(L"launcher", L"ai_modules", ai, g_ini);
    WriteInt(L"player_bonus", GetBonus(g_playerUd));
    WriteInt(L"ai_bonus", GetBonus(g_aiUd));
    WritePrivateProfileStringW(L"launcher", L"hr_block_commandbook", Checked(g_hrBlockCb) ? L"1" : L"0", g_ini);
}

static void StartGame() {
    bool skip = Checked(g_skip), bonus = Checked(g_bonus), takeover = Checked(g_takeover), trap = Checked(g_trap);
    int pb = GetBonus(g_playerUd), ab = GetBonus(g_aiUd);
    wchar_t ai[256];
    AiList(ai);
    SaveSettings();
    wchar_t gameExe[MAX_PATH] = L"";
    if (g_gameDir[0]) {
        if (!GameExeIn(g_gameDir, gameExe)) {
            MessageBoxW(g_wnd, L"设置的游戏目录里找不到三国志14 的程序，请重新选择游戏目录。", APP_TITLE, MB_ICONERROR);
            return;
        }
    } else if (!SteamInstalled()) {
        MessageBoxW(g_wnd, L"没有检测到 Steam。\n如果游戏不是从 Steam 安装的，请点「选择…」指定游戏目录。", APP_TITLE, MB_ICONWARNING);
        return;
    }
    wchar_t hrPath[2 * MAX_PATH];
    HrDllPath(hrPath);
    if (hrPath[0] && !Exists(hrPath)) {
        MessageBoxW(g_wnd, L"找不到设置的人力资源管理器 DLL，请重新选择，或点「清除」不加载它。", APP_TITLE, MB_ICONERROR);
        return;
    }
    bool hr = hrPath[0] != 0;
    if (!skip && !bonus && !takeover && !trap && !ai[0] && !hr) {
        bool ok;
        if (gameExe[0])
            ok = (INT_PTR)ShellExecuteW(g_wnd, L"open", gameExe, nullptr, g_gameDir, SW_SHOWNORMAL) > 32;
        else
            ok = (INT_PTR)ShellExecuteW(g_wnd, L"open", STEAM_URL, nullptr, nullptr, SW_SHOWNORMAL) > 32;
        if (!ok) {
            MessageBoxW(g_wnd, gameExe[0] ? L"无法启动游戏程序。" : L"无法通过 Steam 启动游戏，请确认已安装并登录 Steam。",
                        APP_TITLE, MB_ICONERROR);
            return;
        }
        SetStatus(L"正在启动原版游戏…");
    } else {
        wchar_t py[MAX_PATH], script[MAX_PATH];
        if (!FindPython(py)) {
            MessageBoxW(g_wnd, L"找不到 runtime\\pythonw.exe。请把发布包完整解压后再运行。", APP_TITLE, MB_ICONERROR);
            return;
        }
        if (!FindScript(script)) {
            MessageBoxW(g_wnd, L"找不到 app\\tools\\report_button.py。请把发布包完整解压后再运行。", APP_TITLE, MB_ICONERROR);
            return;
        }
        wchar_t opts[384 + 2 * MAX_PATH] = L"", cmd[2048], cwd[MAX_PATH];
        if (!skip) lstrcatW(opts, L" --no-skip");
        if (bonus) wsprintfW(opts + lstrlenW(opts), L" --area-bonus %d,%d", pb, ab);
        if (takeover) lstrcatW(opts, L" --takeover");
        if (trap) lstrcatW(opts, L" --trap-near");
        if (hr) {
            wsprintfW(opts + lstrlenW(opts), L" --hr-dll \"%s\"", hrPath);
            if (Checked(g_hrBlockCb)) lstrcatW(opts, L" --hr-block commandbook");
        }
        if (ai[0]) wsprintfW(opts + lstrlenW(opts), L" --ai %s", ai);
        if (gameExe[0]) wsprintfW(cmd, L"\"%s\" -P \"%s\"%s \"%s\"", py, script, opts, gameExe);
        else wsprintfW(cmd, L"\"%s\" -P \"%s\"%s --steam", py, script, opts);
        lstrcpyW(cwd, script);
        *wcsrchr(cwd, L'\\') = 0;           // ...\tools
        *wcsrchr(cwd, L'\\') = 0;           // app root
        STARTUPINFOW si = {sizeof(si)};
        PROCESS_INFORMATION pi = {};
        if (!CreateProcessW(nullptr, cmd, nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, cwd, &si, &pi)) {
            wchar_t msg[256];
            wsprintfW(msg, L"启动优化程序失败（错误 %lu）。", GetLastError());
            MessageBoxW(g_wnd, msg, APP_TITLE, MB_ICONERROR);
            return;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        SetStatus(gameExe[0] ? L"正在启动游戏，并加载优化…" : L"正在通过 Steam 启动游戏，并加载优化…");
    }
    EnableWindow(g_start, FALSE);
    SetTimer(g_wnd, ID_TIMER_CLOSE, 2500, nullptr);
}

static HWND Child(const wchar_t* cls, const wchar_t* text, DWORD style, int x, int y, int w, int h, int id, HFONT f) {
    HWND c = CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | style, S(x), S(y), S(w), S(h), g_wnd,
                             (HMENU)(INT_PTR)id, g_inst, nullptr);
    SendMessageW(c, WM_SETFONT, (WPARAM)f, TRUE);
    return c;
}

static void CreateFonts() {
    LOGFONTW lf = {};
    lstrcpyW(lf.lfFaceName, L"Microsoft YaHei UI");
    lf.lfQuality = CLEARTYPE_QUALITY;
    lf.lfHeight = -S(15);
    lf.lfWeight = FW_NORMAL;
    g_font = CreateFontIndirectW(&lf);
    lf.lfHeight = -S(21);
    lf.lfWeight = FW_BOLD;
    g_fontBold = CreateFontIndirectW(&lf);
    lf.lfHeight = -S(13);
    lf.lfWeight = FW_NORMAL;
    g_fontSmall = CreateFontIndirectW(&lf);
    lf.lfHeight = -S(15);
    lf.lfWeight = FW_BOLD;
    g_fontHead = CreateFontIndirectW(&lf);
}

// Layout in 96-dpi units. Client area 460 x 672 (+26 per extra row of AI checkboxes, 4 to a row; + the 人力资源管理器
// and 陷阱 sections); content column x 24..436, option details indented to 46. The sections below 看海 move down by
// TRAP_DY (the 陷阱 section sits right after 看海).
static const int AI_ROWS = (AI_COUNT + 3) / 4, AI_DY = 26 * (AI_ROWS - 1), HR_DY = 136, TRAP_DY = 86;
static const int CW = 460, CH = 672 + AI_DY + HR_DY + TRAP_DY, L0 = 24, L1 = 46, R0 = 436;

// section heading with a thin rule to its right
static HWND Heading(const wchar_t* text, int y) {
    HWND h = Child(L"STATIC", text, SS_LEFT | SS_CENTERIMAGE, L0, y, 76, 22, 0, g_fontHead);
    Child(L"STATIC", L"", SS_ETCHEDHORZ, L0 + 82, y + 11, R0 - L0 - 82, 1, 0, g_fontSmall);
    return h;
}

static HWND NumberBox(int x, int y, int idEdit, int idUd, HWND* ud) {
    HWND e = CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_NUMBER | ES_CENTER,
                             S(x), S(y), S(58), S(28), g_wnd, (HMENU)(INT_PTR)idEdit, g_inst, nullptr);
    SendMessageW(e, WM_SETFONT, (WPARAM)g_font, TRUE);
    SendMessageW(e, EM_SETLIMITTEXT, 3, 0);
    *ud = CreateWindowExW(0, UPDOWN_CLASSW, nullptr,
                          WS_CHILD | WS_VISIBLE | UDS_SETBUDDYINT | UDS_ALIGNRIGHT | UDS_ARROWKEYS | UDS_NOTHOUSANDS,
                          0, 0, 0, 0, g_wnd, (HMENU)(INT_PTR)idUd, g_inst, nullptr);
    SendMessageW(*ud, UDM_SETBUDDY, (WPARAM)e, 0);
    SendMessageW(*ud, UDM_SETRANGE32, 0, 100);
    return e;
}

static int ReadInt(const wchar_t* key, int def) { return (int)GetPrivateProfileIntW(L"launcher", key, def, g_ini); }

static void Layout() {
    Child(L"STATIC", APP_TITLE, SS_LEFT, L0, 16, 300, 30, 0, g_fontBold);
    Child(L"STATIC", APP_VERSION, SS_RIGHT, 340, 24, R0 - 340, 20, 0, g_fontSmall);
    Child(L"STATIC", L"三国志14 威力加强传承版套装 · 不改动游戏文件", SS_LEFT, L0, 48, R0 - L0, 20, 0, g_fontSmall);

    // 回合报告
    g_heads[0] = Heading(L"回合报告", 80);
    g_skip = Child(L"BUTTON", L"跳过每回合开头的「行动报告」「收支报告」", BS_AUTOCHECKBOX | WS_TABSTOP, L0, 108, R0 - L0, 24,
                   ID_SKIP, g_font);
    g_hints[0] = Child(L"STATIC", L"地图左下角的「回看报告」按钮可以随时再看本回合的报告。\n和「优化伴侣」同时开着时，跳过交给它来做。",
                       SS_LEFT, L1, 134, R0 - L1, 38, 0, g_fontSmall);

    // 府加成
    g_heads[1] = Heading(L"府加成", 186);
    g_bonus = Child(L"BUTTON", L"自定义府加成", BS_AUTOCHECKBOX | WS_TABSTOP, L0, 214, R0 - L0, 24, ID_BONUS, g_font);
    g_bonusLabels[0] = Child(L"STATIC", L"我方", SS_LEFT | SS_CENTERIMAGE, L1, 244, 32, 28, 0, g_font);
    g_player = NumberBox(L1 + 34, 244, ID_PLAYER, ID_PLAYER_UD, &g_playerUd);
    g_bonusLabels[1] = Child(L"STATIC", L"电脑", SS_LEFT | SS_CENTERIMAGE, L1 + 108, 244, 32, 28, 0, g_font);
    g_ai = NumberBox(L1 + 142, 244, ID_AI, ID_AI_UD, &g_aiUd);
    g_bonusLabels[2] = Child(L"STATIC", L"预设", SS_LEFT | SS_CENTERIMAGE, 264, 244, 28, 28, 0, g_fontSmall);
    const wchar_t* presetNames[] = {L"高级", L"超级", L"顶级"};
    for (int i = 0; i < 3; i++)
        g_presets[i] = Child(L"BUTTON", presetNames[i], BS_PUSHBUTTON | WS_TABSTOP, 294 + i * 48, 243, 46, 30,
                             ID_PRESET_HIGH + i, g_fontSmall);
    g_hints[1] = Child(L"STATIC", L"电脑的数值在超级、顶级难度下生效，更低难度和我方相同。\n原版：我方 4；电脑 超级 16、顶级 24。",
                       SS_LEFT, L1, 280, R0 - L1, 38, 0, g_fontSmall);

    // 看海
    g_heads[3] = Heading(L"看海", 332);
    g_takeover = Child(L"BUTTON", L"看海时按 Esc 退出看海，接管开局选的势力", BS_AUTOCHECKBOX | WS_TABSTOP, L0, 360, R0 - L0, 24,
                       ID_TAKEOVER, g_font);
    g_hints[2] = Child(L"STATIC", L"看海 = 开局后点系统菜单「自动操作」。按 Esc 后下一旬生效，\n存档再读档可刷新左上角头像。",
                       SS_LEFT, L1, 386, R0 - L1, 38, 0, g_fontSmall);

    // tooltips (the 陷阱 option and every AI module: the description is the tooltip)
    g_tip = CreateWindowExW(WS_EX_TOPMOST, TOOLTIPS_CLASSW, nullptr, WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX,
                            CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, g_wnd, nullptr, g_inst, nullptr);
    SendMessageW(g_tip, TTM_SETMAXTIPWIDTH, 0, S(320));
    SendMessageW(g_tip, TTM_SETDELAYTIME, TTDT_AUTOPOP, 30000);

    // 陷阱 (陷阱近身触发, in san14_ai.dll)
    g_heads[6] = Heading(L"陷阱", 438);
    g_trap = Child(L"BUTTON", L"陷阱近身触发：敌军走到陷阱旁 1 格内才发动", BS_AUTOCHECKBOX | WS_TABSTOP, L0, 466, R0 - L0, 24,
                   ID_TRAP, g_font);
    {
        TOOLINFOW ti = {sizeof(ti)};
        ti.uFlags = TTF_IDISHWND | TTF_SUBCLASS;
        ti.hwnd = g_wnd;
        ti.uId = (UINT_PTR)g_trap;
        ti.lpszText = (LPWSTR)TRAP_TIP;
        SendMessageW(g_tip, TTM_ADDTOOLW, 0, (LPARAM)&ti);
    }
    g_hints[5] = Child(L"STATIC", L"原来 2 格内就发动，效果范围不变。鼠标停在选项上看说明。",
                       SS_LEFT, L1, 492, R0 - L1, 20, 0, g_fontSmall);

    // 电脑 AI (rows of 4 checkboxes; the description of each is its tooltip)
    g_heads[4] = Heading(L"电脑 AI", 438 + TRAP_DY);
    static const int aiX[] = {L0, 130, 232, 334}, aiW[] = {104, 100, 100, R0 - 334};
    wchar_t aiSaved[256];
    GetPrivateProfileStringW(L"launcher", L"ai_modules", L"", aiSaved, 256, g_ini);
    for (int i = 0; i < AI_COUNT; i++) {
        g_aiMods[i] = Child(L"BUTTON", AI_MODULES[i].label, BS_AUTOCHECKBOX | WS_TABSTOP, aiX[i % 4], 466 + TRAP_DY + 26 * (i / 4),
                            aiW[i % 4], 24, ID_AIMOD0 + i, g_font);
        SendMessageW(g_aiMods[i], BM_SETCHECK, InList(aiSaved, AI_MODULES[i].id) ? BST_CHECKED : BST_UNCHECKED, 0);
        TOOLINFOW ti = {sizeof(ti)};
        ti.uFlags = TTF_IDISHWND | TTF_SUBCLASS;
        ti.hwnd = g_wnd;
        ti.uId = (UINT_PTR)g_aiMods[i];
        ti.lpszText = (LPWSTR)AI_MODULES[i].tip;
        SendMessageW(g_tip, TTM_ADDTOOLW, 0, (LPARAM)&ti);
    }
    g_hints[3] = Child(L"STATIC", L"试验功能，对所有电脑势力生效。鼠标停在选项上看说明。\n游戏启动时读入，本局有效；没生效时游戏里会提示。",
                       SS_LEFT, L1, 492 + AI_DY + TRAP_DY, R0 - L1, 38, 0, g_fontSmall);

    // 人力资源管理器 (the user's own copy; loaded from where it is, the game directory stays as it is)
    const int hy = 544 + AI_DY + TRAP_DY;
    g_heads[5] = Child(L"STATIC", L"人力资源管理器", SS_LEFT | SS_CENTERIMAGE, L0, hy, 120, 22, 0, g_fontHead);
    Child(L"STATIC", L"", SS_ETCHEDHORZ, L0 + 126, hy + 11, R0 - L0 - 126, 1, 0, g_fontSmall);
    g_hrText = Child(L"STATIC", L"", SS_LEFT | SS_CENTERIMAGE | SS_PATHELLIPSIS, L0, hy + 28, 262, 30, 0, g_fontSmall);
    Child(L"BUTTON", L"选择…", BS_PUSHBUTTON | WS_TABSTOP, 312, hy + 28, 62, 30, ID_HR_PICK, g_fontSmall);
    g_hrClear = Child(L"BUTTON", L"清除", BS_PUSHBUTTON | WS_TABSTOP, 380, hy + 28, R0 - 380, 30, ID_HR_CLEAR, g_fontSmall);
    g_hrBlockCb = Child(L"BUTTON", L"屏蔽命令书按钮", BS_AUTOCHECKBOX | WS_TABSTOP, L0, hy + 62, R0 - L0, 24, ID_HR_BLOCK_CB,
                        g_font);
    g_hints[4] = Child(L"STATIC", L"选你自己的 dinput8.dll，放在游戏目录以外，启动时一起加载。\n"
                                  L"和跳过报告同开时，它的 F8 跳弹窗等 4 项让给跳过报告。",
                       SS_LEFT, L1, hy + 88, R0 - L1, 38, 0, g_fontSmall);

    // 游戏目录
    const int gy = 544 + AI_DY + HR_DY + TRAP_DY;
    g_heads[2] = Heading(L"游戏目录", gy);
    g_dirText = Child(L"STATIC", L"", SS_LEFT | SS_CENTERIMAGE | SS_PATHELLIPSIS, L0, gy + 28, 262, 30, 0, g_fontSmall);
    Child(L"BUTTON", L"选择…", BS_PUSHBUTTON | WS_TABSTOP, 312, gy + 28, 62, 30, ID_PICK, g_fontSmall);
    g_clear = Child(L"BUTTON", L"清除", BS_PUSHBUTTON | WS_TABSTOP, 380, gy + 28, R0 - 380, 30, ID_CLEAR, g_fontSmall);

    // footer
    Child(L"STATIC", L"", SS_ETCHEDHORZ, 0, gy + 74, CW, 1, 0, g_fontSmall);
    g_status = Child(L"STATIC", L"", SS_LEFT | SS_CENTERIMAGE, L0, gy + 84, 186, 38, 0, g_fontSmall);
    g_start = Child(L"BUTTON", L"启动游戏", BS_DEFPUSHBUTTON | WS_TABSTOP, 216, gy + 84, 120, 38, ID_START, g_font);
    Child(L"BUTTON", L"退出", BS_PUSHBUTTON | WS_TABSTOP, 346, gy + 84, R0 - 346, 38, ID_QUIT, g_font);

    SendMessageW(g_skip, BM_SETCHECK, ReadInt(L"skip_report", 1) ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessageW(g_bonus, BM_SETCHECK, ReadInt(L"area_bonus", 0) ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessageW(g_takeover, BM_SETCHECK, ReadInt(L"takeover", 0) ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessageW(g_trap, BM_SETCHECK, ReadInt(L"trap_near", 0) ? BST_CHECKED : BST_UNCHECKED, 0);
    SetBonus(g_playerUd, ReadInt(L"player_bonus", BONUS_DEFAULT_PLAYER));
    SetBonus(g_aiUd, ReadInt(L"ai_bonus", BONUS_DEFAULT_AI));
    UpdateBonusEnabled();
    GetPrivateProfileStringW(L"launcher", L"game_dir", L"", g_gameDir, MAX_PATH, g_ini);
    ShowGameDir();
    GetPrivateProfileStringW(L"launcher", L"hr_dll", L"", g_hrDll, MAX_PATH, g_ini);
    ShowHrDll();
    SendMessageW(g_hrBlockCb, BM_SETCHECK, ReadInt(L"hr_block_commandbook", 1) ? BST_CHECKED : BST_UNCHECKED, 0);
    SetFocus(g_start);
}

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
    switch (m) {
    case WM_COMMAND:
        switch (LOWORD(wp)) {
        case ID_START: StartGame(); break;
        case ID_QUIT: DestroyWindow(h); break;
        case ID_PICK: PickGameDir(); break;
        case ID_HR_PICK: PickHrDll(); break;
        case ID_HR_CLEAR:
            g_hrDll[0] = 0;
            WritePrivateProfileStringW(L"launcher", L"hr_dll", L"", g_ini);
            ShowHrDll();
            break;
        case ID_CLEAR:
            g_gameDir[0] = 0;
            WritePrivateProfileStringW(L"launcher", L"game_dir", L"", g_ini);
            ShowGameDir();
            break;
        case ID_BONUS: UpdateBonusEnabled(); break;
        case ID_PRESET_HIGH: case ID_PRESET_SUPER: case ID_PRESET_ULTIMATE: {
            static const int ai[] = {4, 16, 24};          // the game's own values per difficulty
            SetBonus(g_playerUd, 4);
            SetBonus(g_aiUd, ai[LOWORD(wp) - ID_PRESET_HIGH]);
            break;
        }
        case ID_PLAYER: case ID_AI:                      // keep typed numbers within 0..100
            if (HIWORD(wp) == EN_KILLFOCUS) SetBonus(LOWORD(wp) == ID_PLAYER ? g_playerUd : g_aiUd,
                                                     GetDlgItemInt(h, LOWORD(wp), nullptr, FALSE));
            break;
        }
        return 0;
    case WM_TIMER:
        if (wp == ID_TIMER_CLOSE) DestroyWindow(h);
        return 0;
    case WM_CTLCOLORSTATIC: {
        HDC dc = (HDC)wp;
        HWND c = (HWND)lp;
        SetBkColor(dc, GetSysColor(COLOR_WINDOW));
        if (c == g_status) SetTextColor(dc, RGB(150, 70, 20));
        else if (c == g_dirText) SetTextColor(dc, g_gameDir[0] ? RGB(30, 30, 30) : RGB(110, 110, 110));
        else if (c == g_hrText) SetTextColor(dc, g_hrDll[0] ? RGB(30, 30, 30) : RGB(110, 110, 110));
        else if (c == g_heads[0] || c == g_heads[1] || c == g_heads[2] || c == g_heads[3] || c == g_heads[4] ||
                 c == g_heads[5] || c == g_heads[6])
            SetTextColor(dc, RGB(128, 40, 30));
        else if (c == g_hints[0] || c == g_hints[1] || c == g_hints[2] || c == g_hints[3] || c == g_hints[4] ||
                 c == g_hints[5])
            SetTextColor(dc, RGB(112, 112, 112));
        return (LRESULT)GetSysColorBrush(COLOR_WINDOW);
    }
    case WM_DESTROY:
        SaveSettings();
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(h, m, wp, lp);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, LPWSTR, int show) {
    g_inst = inst;
    // single instance: bring the existing window to front
    HANDLE mtx = CreateMutexW(nullptr, FALSE, L"Local\\W_SAN14_Launcher");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND other = FindWindowW(L"W_SAN14_Launcher", nullptr);
        if (other) { ShowWindow(other, SW_RESTORE); SetForegroundWindow(other); }
        return 0;
    }
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    GetModuleFileNameW(nullptr, g_dir, MAX_PATH);
    *wcsrchr(g_dir, L'\\') = 0;
    wsprintfW(g_ini, L"%s\\settings.ini", g_dir);

    INITCOMMONCONTROLSEX icc = {sizeof(icc), ICC_STANDARD_CLASSES | ICC_UPDOWN_CLASS | ICC_TAB_CLASSES};
    InitCommonControlsEx(&icc);
    HDC dc = GetDC(nullptr);
    g_dpi = GetDeviceCaps(dc, LOGPIXELSY);
    ReleaseDC(nullptr, dc);
    CreateFonts();

    WNDCLASSEXW wc = {sizeof(wc)};
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1));
    wc.hIconSm = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON, GetSystemMetrics(SM_CXSMICON),
                                   GetSystemMetrics(SM_CYSMICON), 0);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = GetSysColorBrush(COLOR_WINDOW);
    wc.lpszClassName = L"W_SAN14_Launcher";
    RegisterClassExW(&wc);

    DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    RECT r = {0, 0, S(CW), S(CH)};
    AdjustWindowRectEx(&r, style, FALSE, 0);
    int w = r.right - r.left, h = r.bottom - r.top;
    int x = (GetSystemMetrics(SM_CXSCREEN) - w) / 2, y = (GetSystemMetrics(SM_CYSCREEN) - h) / 2;
    g_wnd = CreateWindowExW(0, wc.lpszClassName, APP_TITLE, style, x, y, w, h, nullptr, nullptr, inst, nullptr);
    Layout();
    ShowWindow(g_wnd, show);
    UpdateWindow(g_wnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        if (!IsDialogMessageW(g_wnd, &msg)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    CoUninitialize();
    CloseHandle(mtx);
    return 0;
}
