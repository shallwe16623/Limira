@file:OptIn(ExperimentalLayoutApi::class, ExperimentalMaterial3Api::class)

package com.limira.android.ui

import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Archive
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.FolderOpen
import androidx.compose.material.icons.filled.Login
import androidx.compose.material.icons.filled.Logout
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.PictureAsPdf
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Restore
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.UploadFile
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.limira.android.BuildConfig
import com.limira.android.data.ArtifactBuckets
import com.limira.android.data.GeneratedReport
import com.limira.android.data.LimiraOrganization
import com.limira.android.data.ResearchTask
import com.limira.android.data.UploadedDocument

@Composable
fun LimiraApp(
    pendingDeepLink: Uri?,
    onDeepLinkConsumed: () -> Unit,
    viewModel: LimiraViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsState()
    val context = LocalContext.current

    LaunchedEffect(pendingDeepLink) {
        pendingDeepLink?.let {
            viewModel.handleDeepLink(it)
            onDeepLinkConsumed()
        }
    }

    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize(), color = Color(0xFFF7F9FB)) {
            when {
                state.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
                state.user == null -> AuthScreen(state, viewModel, context)
                else -> WorkbenchScreen(state, viewModel, context)
            }
        }
    }
}

@Composable
private fun AuthScreen(
    state: LimiraUiState,
    viewModel: LimiraViewModel,
    context: Context,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(20.dp)
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Limira", style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Bold)
        Text("旧金山服务器 · 企业管理员优先登录", color = Color(0xFF475569))
        ModeChips(state.authMode, viewModel::setAuthMode)
        when (state.authMode) {
            AuthMode.Enterprise -> EnterpriseLoginForm(state, viewModel)
            AuthMode.Personal -> PersonalLoginForm(state, viewModel)
            AuthMode.Signup -> SignupForm(state, viewModel)
            AuthMode.Reset -> ResetForm(state, viewModel)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            if (state.googleEnabled) {
                Button(onClick = {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(viewModel.mobileOAuthStartUrl("google"))))
                }) {
                    Icon(Icons.Default.Login, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Google")
                }
            }
            if (state.wechatEnabled) {
                Button(onClick = {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(viewModel.mobileOAuthStartUrl("wechat"))))
                }) {
                    Icon(Icons.Default.Login, null)
                    Spacer(Modifier.width(8.dp))
                    Text("微信")
                }
            }
        }
        StatusLine(state)
    }
}

@Composable
private fun ModeChips(mode: AuthMode, onSelect: (AuthMode) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        AuthMode.entries.forEach { item ->
            val label = when (item) {
                AuthMode.Enterprise -> "企业登录"
                AuthMode.Personal -> "个人登录"
                AuthMode.Signup -> "注册"
                AuthMode.Reset -> "重置密码"
            }
            FilterChip(selected = mode == item, onClick = { onSelect(item) }, label = { Text(label) })
        }
    }
}

@Composable
private fun EnterpriseLoginForm(state: LimiraUiState, viewModel: LimiraViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OrganizationPicker(state.organizations, state.selectedOrganizationId, viewModel::setOrganization)
        OutlinedTextField(
            value = state.enterpriseUsername,
            onValueChange = viewModel::setEnterpriseUsername,
            label = { Text("企业用户名") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        PasswordField(state.password, viewModel::setPassword)
        Button(onClick = viewModel::enterpriseLogin, enabled = !state.busy, modifier = Modifier.fillMaxWidth()) {
            Icon(Icons.Default.Login, null)
            Spacer(Modifier.width(8.dp))
            Text("登录企业账号")
        }
    }
}

@Composable
private fun OrganizationPicker(
    organizations: List<LimiraOrganization>,
    selectedId: String,
    onSelect: (String) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    val selected = organizations.firstOrNull { it.id == selectedId }
    Box {
        Button(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) {
            Text(selected?.name ?: "选择组织", maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            organizations.forEach { organization ->
                DropdownMenuItem(
                    text = {
                        Text(
                            listOfNotNull(organization.name, organization.categoryLabel).joinToString(" · "),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    },
                    onClick = {
                        onSelect(organization.id)
                        expanded = false
                    },
                )
            }
        }
    }
}

@Composable
private fun PersonalLoginForm(state: LimiraUiState, viewModel: LimiraViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(
            value = state.personalLogin,
            onValueChange = viewModel::setPersonalLogin,
            label = { Text("用户名或邮箱") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        PasswordField(state.password, viewModel::setPassword)
        Button(onClick = viewModel::personalLogin, enabled = !state.busy, modifier = Modifier.fillMaxWidth()) {
            Icon(Icons.Default.Login, null)
            Spacer(Modifier.width(8.dp))
            Text("登录")
        }
    }
}

@Composable
private fun SignupForm(state: LimiraUiState, viewModel: LimiraViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(state.signupUsername, viewModel::setSignupUsername, label = { Text("用户名") }, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(state.signupEmail, viewModel::setSignupEmail, label = { Text("邮箱") }, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(state.signupName, viewModel::setSignupName, label = { Text("姓名") }, modifier = Modifier.fillMaxWidth())
        PasswordField(state.password, viewModel::setPassword)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = viewModel::signup, enabled = !state.busy) { Text("注册") }
            TextButton(onClick = viewModel::resendVerification) { Text("重发验证邮件") }
        }
    }
}

@Composable
private fun ResetForm(state: LimiraUiState, viewModel: LimiraViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(state.personalLogin, viewModel::setPersonalLogin, label = { Text("邮箱") }, modifier = Modifier.fillMaxWidth())
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = viewModel::requestPasswordReset, enabled = !state.busy) { Text("发送重置邮件") }
        }
        OutlinedTextField(state.resetToken, viewModel::setResetToken, label = { Text("重置 token") }, modifier = Modifier.fillMaxWidth())
        PasswordField(state.password, viewModel::setPassword)
        Button(onClick = viewModel::confirmPasswordReset, enabled = !state.busy) { Text("确认重置") }
    }
}

@Composable
private fun PasswordField(value: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text("密码") },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun WorkbenchScreen(
    state: LimiraUiState,
    viewModel: LimiraViewModel,
    context: Context,
) {
    val resolver = context.contentResolver
    val uploadLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) viewModel.uploadDocument(resolver, uri, displayName(context, uri))
    }
    val speechLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) viewModel.transcribeSpeech(resolver, uri, displayName(context, uri))
    }
    val archiveLauncher = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
        val path = state.activeTask?.downloadUrl
            ?: state.activeTask?.taskId?.let { "/api/limira/tasks/$it/archive.zip" }
        if (uri != null && path != null) viewModel.downloadInto(resolver, uri, path)
    }
    val pdfLauncher = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/pdf")) { uri ->
        viewModel.exportPdfAndDownload(uri, resolver)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Limira")
                        Text(userLabel(state.user), style = MaterialTheme.typography.labelMedium)
                    }
                },
                actions = {
                    IconButton(onClick = viewModel::loadWorkspace) { Icon(Icons.Default.Refresh, "刷新") }
                    IconButton(onClick = viewModel::signOut) { Icon(Icons.Default.Logout, "退出") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item { StatusLine(state) }
            item { ResearchComposer(state, viewModel, speechLauncher::launch) }
            item { TaskHistory(state, viewModel) }
            item {
                ActiveTaskPanel(
                    state = state,
                    viewModel = viewModel,
                    onUpload = { uploadLauncher.launch(arrayOf("application/pdf", "text/*", "text/csv")) },
                    onArchiveDownload = {
                        archiveLauncher.launch("${state.activeTask?.taskId ?: "limira"}-archive.zip")
                    },
                    onPdfDownload = {
                        pdfLauncher.launch("${state.activeTask?.taskId ?: "limira"}-report.pdf")
                    },
                )
            }
            if (state.user?.isEnterpriseAdmin == true) {
                item { AdminPanel(state) }
            }
        }
    }
}

@Composable
private fun StatusLine(state: LimiraUiState) {
    if (state.busy || state.message.isNotBlank()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Color(0xFFEFF6FF), RoundedCornerShape(8.dp))
                .padding(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (state.busy) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(8.dp))
            }
            Text(state.message.ifBlank { "处理中..." }, color = Color(0xFF1E3A8A))
        }
    }
}

@Composable
private fun ResearchComposer(
    state: LimiraUiState,
    viewModel: LimiraViewModel,
    onSpeechPick: (Array<String>) -> Unit,
) {
    Section(title = "研究") {
        ScenarioPicker(state, viewModel)
        OutlinedTextField(
            value = state.query,
            onValueChange = viewModel::setQuery,
            label = { Text("研究问题") },
            minLines = 3,
            modifier = Modifier.fillMaxWidth(),
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = viewModel::submitResearch, enabled = !state.busy) {
                Icon(Icons.Default.Send, null)
                Spacer(Modifier.width(8.dp))
                Text("开始研究")
            }
            TextButton(onClick = viewModel::useScenarioDefault) { Text("使用场景问题") }
            TextButton(onClick = { onSpeechPick(arrayOf("audio/*", "video/webm")) }) {
                Icon(Icons.Default.Mic, null)
                Spacer(Modifier.width(6.dp))
                Text("语音转写")
            }
        }
    }
}

@Composable
private fun ScenarioPicker(state: LimiraUiState, viewModel: LimiraViewModel) {
    var expanded by remember { mutableStateOf(false) }
    val selected = state.scenarios.firstOrNull { it.id == state.selectedScenarioId }
    Box {
        AssistChip(
            onClick = { expanded = true },
            label = { Text(selected?.title ?: "选择场景") },
        )
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            state.scenarios.forEach { scenario ->
                DropdownMenuItem(
                    text = { Text(scenario.title) },
                    onClick = {
                        viewModel.selectScenario(scenario.id)
                        expanded = false
                    },
                )
            }
        }
    }
}

@Composable
private fun TaskHistory(state: LimiraUiState, viewModel: LimiraViewModel) {
    Section(title = "历史") {
        if (state.tasks.isEmpty() && state.archivedTasks.isEmpty()) {
            Text("暂无历史任务", color = Color(0xFF64748B))
        }
        state.tasks.take(8).forEach { task ->
            TaskRow(task, selected = state.activeTask?.taskId == task.taskId, onClick = { viewModel.openTask(task.taskId) })
        }
        if (state.archivedTasks.isNotEmpty()) {
            Text("已归档", style = MaterialTheme.typography.titleSmall)
            state.archivedTasks.take(5).forEach { task ->
                TaskRow(task, selected = false, onClick = { viewModel.openTask(task.taskId) })
            }
        }
    }
}

@Composable
private fun TaskRow(task: ResearchTask, selected: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(if (selected) Color(0xFFDFF7EF) else Color.Transparent, RoundedCornerShape(6.dp))
            .clickable(onClick = onClick)
            .padding(8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column(Modifier.weight(1f)) {
            Text(task.query ?: task.taskId, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("${task.status ?: "unknown"} · ${task.archiveStatus ?: "pending"}", style = MaterialTheme.typography.labelMedium)
        }
        Text(task.conversationCount?.toString() ?: "")
    }
}

@Composable
private fun ActiveTaskPanel(
    state: LimiraUiState,
    viewModel: LimiraViewModel,
    onUpload: () -> Unit,
    onArchiveDownload: () -> Unit,
    onPdfDownload: () -> Unit,
) {
    Section(title = "工作台") {
        val task = state.activeTask
        if (task == null) {
            Text("打开或创建一个任务后查看成果。", color = Color(0xFF64748B))
            return@Section
        }
        Text(task.query ?: task.taskId, fontWeight = FontWeight.SemiBold)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            AssistChip(onClick = {}, label = { Text(task.status ?: "unknown") })
            AssistChip(onClick = {}, label = { Text("归档 ${task.archiveStatus ?: "pending"}") })
            TextButton(onClick = { viewModel.loadTaskDetail() }) { Text("刷新成果") }
            TextButton(onClick = viewModel::archiveActiveHistory) {
                Icon(Icons.Default.Archive, null)
                Text("归档历史")
            }
            TextButton(onClick = viewModel::restoreActiveHistory) {
                Icon(Icons.Default.Restore, null)
                Text("恢复")
            }
        }
        UploadPanel(state, viewModel, onUpload)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onArchiveDownload) {
                Icon(Icons.Default.CloudDownload, null)
                Spacer(Modifier.width(6.dp))
                Text("下载 ZIP")
            }
            Button(onClick = onPdfDownload) {
                Icon(Icons.Default.PictureAsPdf, null)
                Spacer(Modifier.width(6.dp))
                Text("导出 PDF")
            }
        }
        ScrollableTabRow(selectedTabIndex = ArtifactTab.entries.indexOf(state.activeTab), edgePadding = 0.dp) {
            ArtifactTab.entries.forEach { tab ->
                Tab(
                    selected = state.activeTab == tab,
                    onClick = { viewModel.setTab(tab) },
                    text = { Text("${tab.label} ${tabCount(tab, state.artifacts, state.taskLogs)}") },
                )
            }
        }
        ArtifactContent(state)
    }
}

@Composable
private fun UploadPanel(state: LimiraUiState, viewModel: LimiraViewModel, onUpload: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = onUpload) {
                Icon(Icons.Default.UploadFile, null)
                Spacer(Modifier.width(6.dp))
                Text("上传资料")
            }
            TextButton(onClick = viewModel::refreshUploads) {
                Icon(Icons.Default.Refresh, null)
                Text("刷新")
            }
            state.uploadStorage?.let { storage ->
                Text("存储 ${formatBytes(storage.usedBytes)}", style = MaterialTheme.typography.labelMedium)
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = state.uploadSearch,
                onValueChange = viewModel::setUploadSearch,
                label = { Text("检索上传资料") },
                singleLine = true,
                modifier = Modifier.weight(1f),
            )
            IconButton(onClick = viewModel::searchUploads) { Icon(Icons.Default.Search, "搜索") }
        }
        val docs = state.uploadResults.ifEmpty { state.uploads }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            docs.take(8).forEach { doc ->
                AssistChip(
                    onClick = {},
                    label = { Text("${doc.filename ?: doc.documentId} · ${formatBytes(doc.byteSize)}") },
                )
            }
        }
    }
}

@Composable
private fun ArtifactContent(state: LimiraUiState) {
    when (state.activeTab) {
        ArtifactTab.Evidence -> ArtifactCards(state.artifacts.evidence, "证据")
        ArtifactTab.Entities -> ArtifactCards(state.artifacts.entities, "实体")
        ArtifactTab.Graph -> GraphView(state.artifacts)
        ArtifactTab.Timeline -> ArtifactCards(state.artifacts.timelineEvents, "时间线")
        ArtifactTab.Map -> MapView(state.artifacts)
        ArtifactTab.Report -> ReportView(state.artifacts, state.reports)
        ArtifactTab.Logs -> LogView(state)
    }
}

@Composable
private fun ArtifactCards(items: List<Map<String, Any?>>, fallbackTitle: String) {
    if (items.isEmpty()) {
        Text("$fallbackTitle 暂无数据", color = Color(0xFF64748B), modifier = Modifier.padding(12.dp))
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items.forEachIndexed { index, item ->
            Card(colors = CardDefaults.cardColors(containerColor = Color.White), shape = RoundedCornerShape(8.dp)) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(
                        item.textValue("title", "name", "label", "source", "id")
                            ?: "$fallbackTitle ${index + 1}",
                        fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        item.textValue("summary", "description", "text", "markdown") ?: item.toString(),
                        maxLines = 8,
                        overflow = TextOverflow.Ellipsis,
                    )
                    val meta = listOfNotNull(
                        item.textValue("confidence"),
                        item.textValue("published_at"),
                        item.textValue("type", "entity_type", "relation_type"),
                    ).joinToString(" · ")
                    if (meta.isNotBlank()) Text(meta, style = MaterialTheme.typography.labelMedium, color = Color(0xFF64748B))
                }
            }
        }
    }
}

@Composable
private fun GraphView(artifacts: ArtifactBuckets) {
    val nodes = remember(artifacts) { graphNodes(artifacts) }
    val relations = artifacts.relations
    if (nodes.isEmpty() && relations.isEmpty()) {
        Text("图谱暂无数据", color = Color(0xFF64748B), modifier = Modifier.padding(12.dp))
        return
    }
    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(300.dp)
            .background(Color.White, RoundedCornerShape(8.dp)),
    ) {
        val center = Offset(size.width / 2f, size.height / 2f)
        val radius = minOf(size.width, size.height) * 0.36f
        val positions = nodes.mapIndexed { index, node ->
            val angle = (Math.PI * 2 * index / nodes.size - Math.PI / 2).toFloat()
            node.id to Offset(
                x = center.x + kotlin.math.cos(angle) * radius,
                y = center.y + kotlin.math.sin(angle) * radius,
            )
        }.toMap()
        relations.forEach { relation ->
            val source = positions[relation.textValue("source", "from", "source_id")]
            val target = positions[relation.textValue("target", "to", "target_id")]
            if (source != null && target != null) {
                drawLine(Color(0xFF64748B), source, target, strokeWidth = 2f)
            }
        }
        positions.values.forEach { offset ->
            drawCircle(Color(0xFF0F766E), radius = 18f, center = offset)
        }
    }
    ArtifactCards(relations, "关系")
}

@Composable
private fun MapView(artifacts: ArtifactBuckets) {
    val features = remember(artifacts) { mapFeatures(artifacts) }
    if (features.isEmpty()) {
        Text("地图暂无 GeoJSON 坐标", color = Color(0xFF64748B), modifier = Modifier.padding(12.dp))
        return
    }
    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(300.dp)
            .background(Color.White, RoundedCornerShape(8.dp)),
    ) {
        features.forEach { feature ->
            val pairs = collectPairs(feature.geometry["coordinates"])
            if (pairs.isEmpty()) return@forEach
            val projected = pairs.map { (lng, lat) ->
                Offset(
                    x = ((lng + 180.0) / 360.0 * size.width).toFloat(),
                    y = ((90.0 - lat) / 180.0 * size.height).toFloat(),
                )
            }
            val type = feature.geometry["type"]?.toString().orEmpty()
            when {
                type.contains("Polygon") -> {
                    val path = Path().apply {
                        projected.forEachIndexed { index, point ->
                            if (index == 0) moveTo(point.x, point.y) else lineTo(point.x, point.y)
                        }
                        close()
                    }
                    drawPath(path, Color(0x330F766E))
                    drawPath(path, Color(0xFF0F766E), style = Stroke(width = 2f))
                }
                type.contains("LineString") -> projected.zipWithNext().forEach { (a, b) ->
                    drawLine(Color(0xFF0F766E), a, b, strokeWidth = 3f)
                }
                else -> projected.forEach { drawCircle(Color(0xFF2563EB), radius = 7f, center = it) }
            }
        }
    }
    ArtifactCards(features.map { it.source }, "地图")
}

@Composable
private fun ReportView(artifacts: ArtifactBuckets, reports: List<GeneratedReport>) {
    val markdown = reportMarkdown(artifacts)
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (markdown.isBlank()) {
            Text("报告暂无内容", color = Color(0xFF64748B), modifier = Modifier.padding(12.dp))
        } else {
            markdown.split("\n").forEach { line ->
                when {
                    line.startsWith("## ") -> Text(line.removePrefix("## "), fontWeight = FontWeight.Bold)
                    line.isBlank() -> Spacer(Modifier.height(4.dp))
                    else -> Text(line)
                }
            }
        }
        if (reports.isNotEmpty()) {
            Text("已生成 PDF", fontWeight = FontWeight.SemiBold)
            reports.forEach { report ->
                Text("${report.reportId} · ${report.pdfSizeBytes?.let(::formatBytes) ?: "未下载"}")
            }
        }
    }
}

@Composable
private fun LogView(state: LimiraUiState) {
    val logs = state.taskLogs
    if (logs.isEmpty() && state.streamMessages.isEmpty()) {
        Text("暂无日志", color = Color(0xFF64748B), modifier = Modifier.padding(12.dp))
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        state.streamMessages.takeLast(12).forEach {
            Text(
                text = it,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodySmall,
            )
        }
        logs.takeLast(20).forEach {
            Text(
                text = it.toString(),
                maxLines = 4,
                overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun AdminPanel(state: LimiraUiState) {
    Section(title = "企业管理") {
        Text("成员 ${state.enterpriseMembers.size}", fontWeight = FontWeight.SemiBold)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            state.enterpriseMembers.take(12).forEach { member ->
                AssistChip(
                    onClick = {},
                    label = { Text("${member.username ?: member.name ?: member.id} · ${member.organizationRole ?: "member"}") },
                )
            }
        }
        Text("使用量", fontWeight = FontWeight.SemiBold)
        Text(state.enterpriseUsage.toString(), maxLines = 8, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
private fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(Color.White, RoundedCornerShape(8.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        content()
    }
}

private fun userLabel(user: com.limira.android.data.LimiraUser?): String =
    if (user == null) {
        "未登录"
    } else {
        listOfNotNull(
            user.name ?: user.username ?: user.email ?: user.id,
            user.accountType,
            user.organizationRole,
        ).joinToString(" · ")
    }

private fun tabCount(tab: ArtifactTab, artifacts: ArtifactBuckets, logs: List<Map<String, Any?>>): String {
    val count = when (tab) {
        ArtifactTab.Evidence -> artifacts.evidence.size
        ArtifactTab.Entities -> artifacts.entities.size
        ArtifactTab.Graph -> artifacts.entities.size + artifacts.relations.size
        ArtifactTab.Timeline -> artifacts.timelineEvents.size
        ArtifactTab.Map -> mapFeatures(artifacts).size
        ArtifactTab.Report -> artifacts.reportSections.size
        ArtifactTab.Logs -> logs.size
    }
    return if (count > 0) count.toString() else ""
}

private data class GraphNode(val id: String, val label: String)

private fun graphNodes(artifacts: ArtifactBuckets): List<GraphNode> {
    val nodes = linkedMapOf<String, GraphNode>()
    artifacts.entities.forEachIndexed { index, entity ->
        val id = entity.textValue("entity_id", "id", "name") ?: "entity-${index + 1}"
        nodes[id] = GraphNode(id, entity.textValue("name", "label", "entity_id") ?: id)
    }
    artifacts.relations.forEach { relation ->
        listOf("source", "from", "source_id", "target", "to", "target_id").forEach { key ->
            relation[key]?.toString()?.takeIf { it.isNotBlank() }?.let { nodes.putIfAbsent(it, GraphNode(it, it)) }
        }
    }
    return nodes.values.toList()
}

private data class MapFeature(val source: Map<String, Any?>, val geometry: Map<String, Any?>)

private fun mapFeatures(artifacts: ArtifactBuckets): List<MapFeature> =
    (artifacts.mapFeatures + artifacts.timelineEvents).mapNotNull { item ->
        val geometry = (item["geometry"] ?: item["geojson"] ?: (item["payload"] as? Map<*, *>)?.get("geometry"))
        @Suppress("UNCHECKED_CAST")
        when (geometry) {
            is Map<*, *> -> MapFeature(item, geometry as Map<String, Any?>)
            else -> null
        }
    }

private fun collectPairs(value: Any?): List<Pair<Double, Double>> {
    val numbers = (value as? List<*>)?.mapNotNull { (it as? Number)?.toDouble() }
    if (numbers != null && numbers.size >= 2) return listOf(numbers[0] to numbers[1])
    return (value as? List<*>)?.flatMap(::collectPairs).orEmpty()
}

private fun displayName(context: Context, uri: Uri): String {
    var cursor: Cursor? = null
    return try {
        cursor = context.contentResolver.query(uri, null, null, null, null)
        val nameIndex = cursor?.getColumnIndex(OpenableColumns.DISPLAY_NAME) ?: -1
        if (cursor != null && cursor.moveToFirst() && nameIndex >= 0) {
            cursor.getString(nameIndex)
        } else {
            uri.lastPathSegment ?: "upload.bin"
        }
    } finally {
        cursor?.close()
    }
}
