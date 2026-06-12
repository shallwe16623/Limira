package com.limira.android

import com.limira.android.data.ArtifactBuckets
import com.limira.android.ui.evidenceRefs
import com.limira.android.ui.formatBytes
import com.limira.android.ui.reportMarkdown
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LimiraModelTest {
    @Test
    fun reportMarkdownUsesReportSectionsAndEvidenceRefs() {
        val artifacts = ArtifactBuckets(
            reportSections = listOf(
                mapOf(
                    "title" to "结论",
                    "markdown" to "这是报告正文。",
                    "evidence_refs" to listOf("EVID-001", "EVID-002"),
                ),
            ),
        )

        assertTrue(reportMarkdown(artifacts).contains("## 结论"))
        assertEquals(listOf("EVID-001", "EVID-002"), evidenceRefs(artifacts))
    }

    @Test
    fun byteFormatterUsesReadableUnits() {
        assertEquals("42 B", formatBytes(42))
        assertEquals("2.0 KB", formatBytes(2048))
    }
}
