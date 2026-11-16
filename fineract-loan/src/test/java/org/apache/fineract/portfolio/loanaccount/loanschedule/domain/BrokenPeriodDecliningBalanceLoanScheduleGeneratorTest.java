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
package org.apache.fineract.portfolio.loanaccount.loanschedule.domain;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.TreeMap;
import org.apache.fineract.infrastructure.core.exception.GeneralPlatformDomainRuleException;
import org.apache.fineract.organisation.monetary.domain.MonetaryCurrency;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.domain.MoneyHelper;
import org.apache.fineract.organisation.monetary.mapper.CurrencyMapper;
import org.apache.fineract.portfolio.loanaccount.domain.LoanTransactionRepository;
import org.apache.fineract.portfolio.loanaccount.loanschedule.service.BrokenPeriodMethodResolver;
import org.apache.fineract.portfolio.loanproduct.domain.InterestCalculationPeriodMethod;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.AdditionalAnswers;
import org.mockito.MockedStatic;

/**
 * Pinned-value tests for the per-product broken-period first-installment math. The stock (super) computation is driven
 * through a mocked {@link LoanApplicationTerms} whose interest/principal calculators return the plain 30-day-month
 * values, matching what the real cumulative generator produces under SAME_AS_REPAYMENT_PERIOD.
 *
 * Golden vectors come from DMI_Logic_Validation.xlsx (5,143 real DMI schedules) and the Vivriti parity harness.
 */
public class BrokenPeriodDecliningBalanceLoanScheduleGeneratorTest {

    // Tenant convention: rounding-mode 4 (HALF_UP), INR with 2 decimals.
    private static final MockedStatic<MoneyHelper> MONEY_HELPER = mockStatic(MoneyHelper.class);
    private static final MathContext MC = new MathContext(19, RoundingMode.HALF_UP);
    private static final MonetaryCurrency INR = new MonetaryCurrency("INR", 2, 0);
    private static final Long PRODUCT_ID = 7L;

    private BrokenPeriodMethodResolver resolver;
    private LoanApplicationTerms terms;
    private BrokenPeriodDecliningBalanceLoanScheduleGenerator generator;

    @BeforeEach
    public void setUp() {
        MONEY_HELPER.when(MoneyHelper::getRoundingMode).thenReturn(RoundingMode.HALF_UP);
        MONEY_HELPER.when(MoneyHelper::getMathContext).thenReturn(MC);
        resolver = mock(BrokenPeriodMethodResolver.class);
        terms = mock(LoanApplicationTerms.class);
        generator = new BrokenPeriodDecliningBalanceLoanScheduleGenerator(mock(ScheduledDateGenerator.class),
                mock(PaymentPeriodsInOneYearCalculator.class), mock(LoanTransactionRepository.class), mock(CurrencyMapper.class), resolver);
    }

    @AfterAll
    public static void tearDown() {
        MONEY_HELPER.close();
    }

    private void givenLoan(final BigDecimal principal, final BigDecimal annualRate, final int tenure, final LocalDate interestStart,
            final String method) {
        when(resolver.resolve(PRODUCT_ID)).thenReturn(method);
        when(terms.getLoanProductId()).thenReturn(PRODUCT_ID);
        when(terms.getInterestCalculationPeriodMethod()).thenReturn(InterestCalculationPeriodMethod.SAME_AS_REPAYMENT_PERIOD);
        when(terms.getExpectedDisbursementDate()).thenReturn(interestStart);
        when(terms.getAnnualNominalInterestRate()).thenReturn(annualRate);
        when(terms.getNumberOfRepayments()).thenReturn(tenure);
        when(terms.getPrincipalGrace()).thenReturn(0);
        // Stock SAME_AS_REPAYMENT period-1 values: interest = OB * rate/1200, principal = unroundedEMI - interest.
        final BigDecimal monthlyRate = annualRate.divide(BigDecimal.valueOf(1200), MC);
        final BigDecimal stockInterest = principal.multiply(monthlyRate, MC);
        final BigDecimal pow = BigDecimal.ONE.add(monthlyRate).pow(tenure, MC);
        final BigDecimal emiUnrounded = principal.multiply(monthlyRate, MC)
                .divide(BigDecimal.ONE.subtract(BigDecimal.ONE.divide(pow, MC)), MC);
        when(terms.calculateTotalInterestForPeriod(any(), any(), anyInt(), any(), any(), any(), any(), any()))
                .thenReturn(new PrincipalInterest(Money.zero(INR, MC), Money.of(INR, stockInterest, MC), Money.zero(INR, MC)));
        when(terms.calculateTotalPrincipalForPeriod(any(), any(), anyInt(), any(), any()))
                .thenReturn(Money.of(INR, emiUnrounded.subtract(stockInterest), MC));
        when(terms.adjustPrincipalIfLastRepaymentPeriod(any(), any(), anyInt())).thenAnswer(AdditionalAnswers.returnsFirstArg());
    }

    private PrincipalInterest generate(final BigDecimal principal, final LocalDate periodStart, final LocalDate periodEnd,
            final int periodNumber) {
        final Money outstanding = Money.of(INR, principal, MC);
        return generator.calculatePrincipalInterestComponentsForPeriod(mock(PaymentPeriodsInOneYearCalculator.class), BigDecimal.ZERO,
                Money.zero(INR, MC), Money.zero(INR, MC), Money.zero(INR, MC), Money.zero(INR, MC), outstanding, terms, periodNumber, MC,
                new TreeMap<>(), new HashMap<>(), periodStart, periodEnd, new ArrayList<>());
    }

    // --- DMI: ADJUSTED_DAYS_SYMMETRIC ---

    @Test
    public void dmiLongBrokenPeriodSurchargesFirstInstallment() {
        // xlsx golden: 312000 @22%/36m, interest start 21-Apr-2026 (30d month), first coupon 05-Jun-2026:
        // raw 45 days, adjusted 45; EMI 11915.42, first installment 14775.42 (interest 8580.00, principal 6195.42).
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        assertEquals(new BigDecimal("8580.00"), result.interest().getAmount());
        assertEquals(new BigDecimal("6195.42"), result.principal().getAmount());
    }

    @Test
    public void dmiShortBrokenPeriodShrinksFirstInstallmentBelowEmi() {
        // xlsx golden: 320000 @23%/36m, interest start 14-Jul-2025 (31d month -> -1), first coupon 05-Aug-2025:
        // raw 22 days, adjusted 21; EMI 12387.11, first installment 10547.11 (interest 4293.33, principal 6253.78).
        final BigDecimal principal = BigDecimal.valueOf(320000);
        givenLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2025, 7, 14), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 7, 5), LocalDate.of(2025, 8, 5), 1);
        assertEquals(new BigDecimal("4293.33"), result.interest().getAmount());
        assertEquals(new BigDecimal("6253.78"), result.principal().getAmount());
    }

    @Test
    public void dmiFebruaryStartAddsTwoDays() {
        // 300000 @22%/36m, interest start 10-Feb-2026 (28d month -> +2), first coupon 05-Mar-2026:
        // raw 23 days, adjusted 25; interest = 300000*22/36000*25 = 4583.33.
        final BigDecimal principal = BigDecimal.valueOf(300000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 2, 10), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 2, 5), LocalDate.of(2026, 3, 5), 1);
        assertEquals(new BigDecimal("4583.33"), result.interest().getAmount());
    }

    @Test
    public void dmiAdjustedThirtyDayBoundaryReturnsStock() {
        // Interest start 05-Dec-2026 (31d month): raw 31, adjusted 30 -> stock result must be returned as-is.
        final BigDecimal principal = BigDecimal.valueOf(300000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 12, 5), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 12, 5), LocalDate.of(2027, 1, 5), 1);
        // stock 30-day interest = 300000*22/1200 = 5500.00
        assertEquals(new BigDecimal("5500.00"), result.interest().getAmount());
    }

    @Test
    public void dmiAnchorsOnInterestChargedFromDateWhenPresent() {
        // Disbursed 18-Apr but interest runs from 21-Apr: same numbers as the long-broken-period golden.
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 18), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        when(terms.getInterestChargedFromLocalDate()).thenReturn(LocalDate.of(2026, 4, 21));
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        assertEquals(new BigDecimal("8580.00"), result.interest().getAmount());
        assertEquals(new BigDecimal("6195.42"), result.principal().getAmount());
    }

    // --- Legacy (null method) and Vivriti (ACTUAL_DAYS_SURCHARGE) ---

    @Test
    public void legacyShortBrokenPeriodKeepsInstallmentEqualToEmi() {
        // Vivriti-style: 420000 @21.18%/24m disbursed 10-Jan-2025, first EMI 05-Feb-2025 -> 26 actual days.
        // Interest = 420000*21.18/36000*26 = 6424.60; installment stays = stock EMI (interest saved moves to
        // principal).
        final BigDecimal principal = BigDecimal.valueOf(420000);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), null);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        assertEquals(new BigDecimal("6424.60"), result.interest().getAmount());
        // installment == stock EMI: principal + interest must equal the stock principal + stock 30-day interest
        final BigDecimal stockEmi = new BigDecimal("14206.12").add(new BigDecimal("7413.00")); // unroundedEMI split
                                                                                               // @21.18%/24m
        assertEquals(stockEmi, result.principal().getAmount().add(result.interest().getAmount()));
    }

    @Test
    public void actualDaysSurchargeMatchesLegacyBehaviour() {
        final BigDecimal principal = BigDecimal.valueOf(420000);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), BrokenPeriodMethodResolver.ACTUAL_DAYS_SURCHARGE);
        final PrincipalInterest explicit = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        givenLoan(principal, BigDecimal.valueOf(21.18), 24, LocalDate.of(2025, 1, 10), null);
        final PrincipalInterest legacy = generate(principal, LocalDate.of(2025, 1, 5), LocalDate.of(2025, 2, 5), 1);
        assertEquals(legacy.interest().getAmount(), explicit.interest().getAmount());
        assertEquals(legacy.principal().getAmount(), explicit.principal().getAmount());
    }

    @Test
    public void legacyThirtyOneDayMonthDoesNotNormalize() {
        // Same dates as the DMI short golden but with NO config row: legacy counts 22 ACTUAL days (no -1).
        final BigDecimal principal = BigDecimal.valueOf(320000);
        givenLoan(principal, BigDecimal.valueOf(23), 36, LocalDate.of(2025, 7, 14), null);
        final PrincipalInterest result = generate(principal, LocalDate.of(2025, 7, 5), LocalDate.of(2025, 8, 5), 1);
        // 320000*23/36000*22 = 4497.78 (not the DMI 4293.33)
        assertEquals(new BigDecimal("4497.78"), result.interest().getAmount());
    }

    // --- Dispatch edges ---

    @Test
    public void standardMethodReturnsStockUntouched() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.STANDARD);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1);
        // stock values pass through even though the broken period is 45 days
        assertEquals(new BigDecimal("5720.00"), result.interest().getAmount());
    }

    @Test
    public void unknownMethodFailsLoudly() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), "ADJUSTED_DAY_SYMMETRIC"); // typo
        assertThrows(GeneralPlatformDomainRuleException.class,
                () -> generate(principal, LocalDate.of(2026, 5, 5), LocalDate.of(2026, 6, 5), 1));
    }

    @Test
    public void laterPeriodsNeverConsultTheResolver() {
        final BigDecimal principal = BigDecimal.valueOf(312000);
        givenLoan(principal, BigDecimal.valueOf(22), 36, LocalDate.of(2026, 4, 21), BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC);
        final PrincipalInterest result = generate(principal, LocalDate.of(2026, 6, 5), LocalDate.of(2026, 7, 5), 2);
        assertEquals(new BigDecimal("5720.00"), result.interest().getAmount());
        verifyNoInteractions(resolver);
    }
}
