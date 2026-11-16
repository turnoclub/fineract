/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.fineract.portfolio.loanaccount.loanschedule.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.LocalDate;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.ResultSetExtractor;

public class CutoffFirstRepaymentDateResolverTest {

    private JdbcTemplate jdbcTemplate;
    private CutoffFirstRepaymentDateResolver resolver;

    @BeforeEach
    public void setUp() {
        jdbcTemplate = mock(JdbcTemplate.class);
        resolver = new CutoffFirstRepaymentDateResolver(jdbcTemplate);
    }

    private void givenConfig(final int installmentDay, final int cutoffDay) {
        when(jdbcTemplate.query(anyString(), any(ResultSetExtractor.class), eq(7L)))
                .thenReturn(new int[] { installmentDay, cutoffDay });
    }

    // DMI convention: installment day 5, cutoff 20.
    @Test
    public void anchorOnCutoffDayGetsNextMonth() {
        givenConfig(5, 20);
        assertEquals(LocalDate.of(2026, 2, 5), resolver.resolve(7L, LocalDate.of(2026, 1, 20)));
    }

    @Test
    public void anchorAfterCutoffDaySkipsAMonth() {
        givenConfig(5, 20);
        assertEquals(LocalDate.of(2026, 3, 5), resolver.resolve(7L, LocalDate.of(2026, 1, 21)));
    }

    // Vivriti convention: installment day 5, cutoff 15 (regression).
    @Test
    public void vivritiCutoffFifteen() {
        givenConfig(5, 15);
        assertEquals(LocalDate.of(2025, 2, 5), resolver.resolve(7L, LocalDate.of(2025, 1, 10)));
        assertEquals(LocalDate.of(2025, 3, 5), resolver.resolve(7L, LocalDate.of(2025, 1, 16)));
    }

    @Test
    public void installmentDayIsClampedToMonthLength() {
        givenConfig(31, 20);
        assertEquals(LocalDate.of(2026, 2, 28), resolver.resolve(7L, LocalDate.of(2026, 1, 15)));
    }

    @Test
    public void returnsNullWithoutConfigRow() {
        when(jdbcTemplate.query(anyString(), any(ResultSetExtractor.class), eq(7L))).thenReturn(null);
        assertNull(resolver.resolve(7L, LocalDate.of(2026, 1, 15)));
    }

    @Test
    public void returnsNullWhenDatatableIsAbsent() {
        when(jdbcTemplate.query(anyString(), any(ResultSetExtractor.class), eq(7L)))
                .thenThrow(new DataAccessResourceFailureException("table missing"));
        assertNull(resolver.resolve(7L, LocalDate.of(2026, 1, 15)));
    }

    @Test
    public void returnsNullForNullInputsWithoutQuerying() {
        assertNull(resolver.resolve(null, LocalDate.of(2026, 1, 15)));
        assertNull(resolver.resolve(7L, null));
        verifyNoInteractions(jdbcTemplate);
    }
}
